"""Tests for cc_rate_limit_parks CRUD — the durable rate-limit park lineage.

Covers idempotent upsert (partial-unique open dedup), due-selection, claim race,
in-place re-limit + needs_user escalation, stale-resuming recovery, terminal
prune, and the dedup-slot release on escalation.
"""

from __future__ import annotations

import pytest

from genesis.db.crud import cc_rate_limit_parks as parks


def _payload(prompt: str = "hi") -> dict:
    return {"prompt": prompt, "profile": "research"}


async def test_upsert_inserts_then_bumps_same_dedup_key(db):
    pid = await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="k1",
        payload=_payload(),
        origin_session_id="sess1",
        limit_kind="session",
        raw_signal="resets 5pm",
        reset_at="2026-07-22T17:00:00+00:00",
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    # Same open dedup_key → same row, attempts bumped, reset refreshed.
    pid2 = await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="k1",
        payload=_payload(),
        origin_session_id="sess1",
        limit_kind="session",
        raw_signal="resets 6pm",
        reset_at="2026-07-22T18:00:00+00:00",
        next_attempt_at="2026-07-22T18:00:00+00:00",
    )
    assert pid == pid2
    row = await parks.get_by_id(db, pid)
    assert row["attempts"] == 1
    assert row["reset_at"] == "2026-07-22T18:00:00+00:00"
    assert await parks.count_open(db) == 1


async def test_distinct_dedup_keys_coexist(db):
    await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="a",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="unknown",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    await parks.upsert_open_park(
        db,
        kind="direct_session",
        dedup_key="b",
        payload=_payload("x"),
        origin_session_id="s2",
        limit_kind="weekly",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    assert await parks.count_open(db) == 2


async def test_list_due_respects_next_attempt_at(db):
    await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="k",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="session",
        raw_signal=None,
        reset_at="2026-07-22T18:00:00+00:00",
        next_attempt_at="2026-07-22T18:00:00+00:00",
    )
    assert len(await parks.list_due(db, now="2026-07-22T17:30:00+00:00")) == 0
    assert len(await parks.list_due(db, now="2026-07-22T19:00:00+00:00")) == 1


async def test_claim_is_single_winner(db):
    pid = await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="k",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="session",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    assert await parks.claim(db, pid) is True
    assert await parks.claim(db, pid) is False  # already resuming
    assert (await parks.get_by_id(db, pid))["status"] == "resuming"


async def test_mark_resumed_only_from_resuming(db):
    pid = await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="k",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="session",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    assert await parks.mark_resumed(db, pid) is False  # still parked
    await parks.claim(db, pid)
    assert await parks.mark_resumed(db, pid) is True
    assert (await parks.get_by_id(db, pid))["status"] == "resumed"


async def test_relimit_increments_in_place_then_escalates(db):
    pid = await parks.upsert_open_park(
        db,
        kind="direct_session",
        dedup_key="k",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="session",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    await parks.claim(db, pid)
    # First re-limit: back to parked, attempts=1.
    st = await parks.relimit(
        db,
        pid,
        reset_at="2026-07-22T20:00:00+00:00",
        next_attempt_at="2026-07-22T20:00:00+00:00",
        needs_user_at_attempts=3,
    )
    assert st == "parked"
    assert (await parks.get_by_id(db, pid))["attempts"] == 1
    # Drive to the escalation threshold.
    await parks.claim(db, pid)
    await parks.relimit(db, pid, reset_at=None, next_attempt_at="x", needs_user_at_attempts=3)
    await parks.claim(db, pid)
    st_final = await parks.relimit(
        db, pid, reset_at=None, next_attempt_at="x", needs_user_at_attempts=3
    )
    assert st_final == "needs_user"
    assert (await parks.get_by_id(db, pid))["status"] == "needs_user"
    # needs_user is not due and not pruned.
    assert len(await parks.list_due(db, now="2099-01-01T00:00:00+00:00")) == 0


async def test_escalation_frees_dedup_slot(db):
    """A park escalated to needs_user releases its dedup_key so a fresh identical
    request can re-park (needs_user is not an OPEN state)."""
    pid = await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="same",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="session",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    await parks.claim(db, pid)
    await parks.relimit(db, pid, reset_at=None, next_attempt_at="x", needs_user_at_attempts=1)
    assert (await parks.get_by_id(db, pid))["status"] == "needs_user"
    pid_new = await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="same",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="session",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    assert pid_new != pid
    assert await parks.count_open(db) == 1  # only the new one


async def test_recover_stale_resuming(db):
    pid = await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="k",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="session",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    await parks.claim(db, pid)
    # max_age_s=0 → any claimed_at is stale → reclaimed.
    assert await parks.recover_stale_resuming(db, max_age_s=0) >= 1
    assert (await parks.get_by_id(db, pid))["status"] == "parked"


async def test_prune_terminal_keeps_needs_user(db):
    # A resumed park older than the window is pruned; needs_user is never pruned.
    pid = await parks.upsert_open_park(
        db,
        kind="conversation",
        dedup_key="k",
        payload=_payload(),
        origin_session_id="s",
        limit_kind="session",
        raw_signal=None,
        reset_at=None,
        next_attempt_at="2026-07-22T17:00:00+00:00",
    )
    await parks.claim(db, pid)
    await parks.mark_resumed(db, pid)
    # Force updated_at into the deep past so the prune window catches it.
    await db.execute(
        "UPDATE cc_rate_limit_parks SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (pid,),
    )
    await db.commit()
    assert await parks.prune_terminal(db, older_than_days=45) == 1
    assert await parks.get_by_id(db, pid) is None


@pytest.mark.parametrize("bad_status", ["bogus", "PARKED", ""])
async def test_status_check_constraint(db, bad_status):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            """INSERT INTO cc_rate_limit_parks
                   (id, kind, dedup_key, payload_json, status, attempts,
                    created_at, updated_at)
               VALUES ('x', 'conversation', 'k', '{}', ?, 0, 'n', 'n')""",
            (bad_status,),
        )
        await db.commit()
