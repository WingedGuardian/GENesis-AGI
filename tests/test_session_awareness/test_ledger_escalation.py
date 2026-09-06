"""Tests for the undisposed-ledger escalation sweep.

Under test is the ESCALATION DECISION and its reverse sync: which unresolved
ledger rows become `user_input_needed` follow-ups once their owning session can
no longer be asked to dispose of them, and which are correctly left alone.

The acceptance cell is `test_acceptance_replays_the_measured_live_shape`, which
reconstructs the real 2026-09-06 live state (a 15-day-old user-world errand in a
long-dead session) rather than a stylised approximation.

Fixtures are synthetic throughout — no real session ids, paths or row text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.crud import follow_ups as fu_crud
from genesis.db.crud import session_charters as sc_crud
from genesis.db.schema import create_all_tables
from genesis.session_awareness import ledger_escalation as esc
from genesis.session_awareness.ledger_escalation_link import (
    ESCALATION_SOURCE,
    escalation_dedup_key,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

# The sweep's own defaults, restated so a test reads independently of the yaml.
CFG = {
    "enabled": True,
    "stale_days": 5,
    "quiet_days": 5,
    "max_per_run": 5,
    "priority": "high",
    "escalate_added_by": ["foreground"],
}


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def sessions_dir(tmp_path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return d


async def _mk_row(
    db,
    *,
    session_id: str = "sess-aaaa",
    text: str = "a thing we agreed to do",
    added_by: str = "foreground",
    created_days_ago: float = 30,
    updated_days_ago: float | None = None,
    status: str = "open",
) -> str:
    """A ledger row with back-dated timestamps (raw UPDATE — ledger_add stamps now)."""
    item_id = await sc_crud.ledger_add(db, session_id=session_id, text=text, added_by=added_by)
    created = (NOW - timedelta(days=created_days_ago)).isoformat()
    updated = (
        (NOW - timedelta(days=updated_days_ago)).isoformat()
        if updated_days_ago is not None
        else None
    )
    await db.execute(
        "UPDATE session_ledger SET created_at=?, updated_at=?, status=? WHERE id=?",
        (created, updated, status, item_id),
    )
    await db.commit()
    return item_id


def _mk_liveness(sessions_dir: Path, session_id: str, *, days_ago: float) -> Path:
    """A last_prompt_time file recording a prompt `days_ago` days back."""
    d = sessions_dir / session_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / "last_prompt_time"
    f.write_text((NOW - timedelta(days=days_ago)).isoformat())
    return f


async def _escalations(db) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM follow_ups WHERE source = ? ORDER BY created_at", (ESCALATION_SOURCE,)
    )
    return [dict(r) for r in await cur.fetchall()]


async def _sweep(db, sessions_dir, **overrides):
    cfg = {**CFG, **overrides}
    return await esc.run_sweep(db, now=NOW, sessions_dir=sessions_dir, cfg=cfg)


# ---------------------------------------------------------------- acceptance


async def test_acceptance_replays_the_measured_live_shape(db, sessions_dir):
    """The real defect: a user-requested errand invisible for 15 days.

    Reconstructed from the live ledger read on 2026-09-06 — a row untouched 15
    days whose owning session last prompted 11.7 days ago. Nothing else on the
    box surfaces it: it is not repo work, not a follow-up, and its session is
    dead so the injection that named it never renders again.
    """
    row_id = await _mk_row(
        db,
        session_id="sess-dead",
        text="NEXT TASK (user-requested): diagnose the failing network share mount",
        created_days_ago=15,
    )
    _mk_liveness(sessions_dir, "sess-dead", days_ago=11.7)

    result = await _sweep(db, sessions_dir)

    assert result["created"] == 1
    rows = await _escalations(db)
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == escalation_dedup_key(row_id)
    assert rows[0]["strategy"] == "user_input_needed"
    assert "network share mount" in rows[0]["content"]


# ------------------------------------------------------- the double threshold


async def test_stale_row_in_a_QUIET_session_escalates(db, sessions_dir):
    await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    assert (await _sweep(db, sessions_dir))["created"] == 1


async def test_stale_row_in_an_ACTIVE_session_is_left_alone(db, sessions_dir):
    """The row is that session's to dispose — escalating takes the decision
    away from the one party still able to make it."""
    await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=0.01)
    result = await _sweep(db, sessions_dir)
    assert result["created"] == 0
    assert result["skipped_active"] == 1
    assert await _escalations(db) == []


async def test_fresh_row_in_a_quiet_session_is_left_alone(db, sessions_dir):
    await _mk_row(db, session_id="s1", created_days_ago=1)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    assert (await _sweep(db, sessions_dir))["created"] == 0


async def test_updated_at_restarts_the_staleness_clock(db, sessions_dir):
    """Any ledger_update bumps updated_at — a row someone is working never qualifies."""
    await _mk_row(db, session_id="s1", created_days_ago=30, updated_days_ago=1)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    assert (await _sweep(db, sessions_dir))["created"] == 0


# ------------------------------------------------------------ liveness signal


async def test_absent_liveness_file_counts_as_quiet_and_says_so(db, sessions_dir):
    """Dispatched sessions write no last_prompt_time; they are unattended by
    definition, so the row's own age governs — and the follow-up states that."""
    await _mk_row(db, session_id="dispatched", created_days_ago=30)
    result = await _sweep(db, sessions_dir)
    assert result["created"] == 1
    assert "no liveness file" in (await _escalations(db))[0]["content"]


async def test_content_is_preferred_over_mtime(db, sessions_dir):
    """A restore from backup rewrites every file with a FRESH mtime while
    preserving the recorded instant. Trusting mtime would make every dead
    session look live and silence the sweep entirely."""
    await _mk_row(db, session_id="s1", created_days_ago=30)
    f = _mk_liveness(sessions_dir, "s1", days_ago=30)
    import os
    import time

    os.utime(f, (time.time(), time.time()))  # simulate the restore: mtime = now

    assert (await _sweep(db, sessions_dir))["created"] == 1, (
        "mtime said 'live', content said 'quiet 30d' — content must win"
    )


async def test_unparseable_liveness_falls_back_to_mtime(db, sessions_dir):
    await _mk_row(db, session_id="s1", created_days_ago=30)
    d = sessions_dir / "s1"
    d.mkdir(parents=True)
    f = d / "last_prompt_time"
    f.write_text("not a timestamp")
    import os

    old = (NOW - timedelta(days=30)).timestamp()
    os.utime(f, (old, old))

    result = await _sweep(db, sessions_dir)
    assert result["created"] == 1
    assert "mtime" in (await _escalations(db))[0]["content"]


async def test_row_with_unparseable_timestamps_is_skipped_not_escalated(db, sessions_dir, caplog):
    """The value must sort BELOW the cutoff to reach the Python skip branch.

    `ledger_stale_open` compares timestamps as STRINGS. A value like 'garbage'
    sorts ABOVE an ISO cutoff ('g' > '2'), so SQL filters it out and the branch
    under test is never entered — the test would then pass identically with that
    branch deleted. '' sorts below everything and does reach it.
    """
    item_id = await _mk_row(db, session_id="s1", created_days_ago=30)
    await db.execute(
        "UPDATE session_ledger SET created_at='', updated_at=NULL WHERE id=?",
        (item_id,),
    )
    await db.commit()
    _mk_liveness(sessions_dir, "s1", days_ago=30)

    with caplog.at_level("WARNING", logger=esc.logger.name):
        assert (await _sweep(db, sessions_dir))["created"] == 0
    assert "no parseable timestamp" in caplog.text


async def test_a_corrupt_liveness_file_does_not_kill_the_sweep(db, sessions_dir):
    """UnicodeDecodeError is a ValueError, NOT an OSError. Uncaught it escapes
    run_sweep entirely, so ONE unrelated session's truncated state file would
    stop every escalation and every reconcile, hourly, forever."""
    await _mk_row(db, session_id="corrupt", created_days_ago=30)
    await _mk_row(db, session_id="healthy", created_days_ago=30)
    import os

    d = sessions_dir / "corrupt"
    d.mkdir(parents=True)
    f = d / "last_prompt_time"
    f.write_bytes(b"\xff\xfe\x00truncated")
    # Back-date the mtime: with errors="replace" the unparseable CONTENT falls
    # through to the mtime branch, so a file written just now would read as a
    # live session and be held back for the right reason — masking whether the
    # sweep survived the decode at all.
    old = (NOW - timedelta(days=30)).timestamp()
    os.utime(f, (old, old))
    _mk_liveness(sessions_dir, "healthy", days_ago=30)

    result = await _sweep(db, sessions_dir)
    assert result["created"] == 2, "the healthy session must not be collateral"

    # Two INDEPENDENT guards cover the decode, and the survival assertion above
    # is satisfied by either alone (mutation-verified), so it pins neither:
    #   * errors="replace" stops read_text raising at all, which keeps the MTIME
    #     fallback reachable — a partially-corrupt file still yields its real
    #     last-write time instead of being written off as absent;
    #   * except ValueError catches a decode error that slips past it.
    # This is the assertion that separates them: only errors="replace" produces
    # the mtime attribution. Without it the row still escalates, but for the
    # WRONG reason ("no liveness file"), discarding evidence we had.
    corrupt_row = next(e for e in await _escalations(db) if e["source_session"] == "corrupt")
    assert "mtime" in corrupt_row["content"], (
        "a decodable-with-replacement file must fall back to mtime, not be "
        "treated as if no liveness file existed"
    )


# --------------------------------------------------------------- row scoping


async def test_in_progress_rows_are_escalatable(db, sessions_dir):
    """in_progress means someone started and never finished — exactly the state
    worth escalating, not an exemption from it."""
    await _mk_row(db, session_id="s1", created_days_ago=30, status="in_progress")
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    assert (await _sweep(db, sessions_dir))["created"] == 1


@pytest.mark.parametrize("status", ["done", "absorbed", "dropped"])
async def test_disposed_rows_are_never_escalated(db, sessions_dir, status):
    await _mk_row(db, session_id="s1", created_days_ago=30, status=status)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    assert (await _sweep(db, sessions_dir))["created"] == 0


async def test_ambient_extractor_rows_are_excluded_by_default(db, sessions_dir):
    """Extractor rows are PROPOSALS, not agreements a human made — asking the
    owner to dispose of something nobody committed to is noise."""
    await _mk_row(db, session_id="s1", created_days_ago=30, added_by="ambient_ledger_extractor")
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    assert (await _sweep(db, sessions_dir))["created"] == 0


async def test_allow_list_can_be_widened_deliberately(db, sessions_dir):
    await _mk_row(db, session_id="s1", created_days_ago=30, added_by="ambient_ledger_extractor")
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    result = await _sweep(
        db, sessions_dir, escalate_added_by=["foreground", "ambient_ledger_extractor"]
    )
    assert result["created"] == 1


# ------------------------------------------------------------- cap & ordering


async def test_cap_takes_the_oldest_and_reports_the_deferred(db, sessions_dir):
    ids = []
    for i in range(8):
        ids.append(await _mk_row(db, session_id="s1", text=f"item {i}", created_days_ago=30 - i))
    _mk_liveness(sessions_dir, "s1", days_ago=30)

    result = await _sweep(db, sessions_dir, max_per_run=3)

    assert result["created"] == 3
    assert result["deferred"] == 5
    assert len(result["deferred_ids"]) == 5
    created_keys = {r["dedup_key"] for r in await _escalations(db)}
    # Oldest three (created_days_ago 30, 29, 28) escalate; the rest are deferred.
    assert created_keys == {escalation_dedup_key(i) for i in ids[:3]}
    assert set(result["deferred_ids"]) == set(ids[3:])


async def test_the_backlog_drains_across_runs(db, sessions_dir):
    """The regression test for a starvation bug a single-run test cannot see.

    The sweep deliberately never writes session_ledger, so an already-escalated
    row keeps its open status and old timestamp: it re-qualifies every run and,
    under oldest-first ordering, sits at the FRONT of the candidate list. If the
    per-run cap were applied BEFORE the dedup filter, those rows would consume
    the entire budget forever and rows past the cap would never escalate.

    MEASURED on the pre-fix code: created 3, 0, 0, 0, 0 across five runs, while
    logging "they escalate on later runs" every time. Note that the shape which
    camouflages it — a re-run creating nothing — is ALSO what correct idempotency
    looks like on a single row, which is why only a multi-run, multi-row probe
    discriminates.
    """
    for i in range(8):
        await _mk_row(db, session_id="s1", text=f"item {i}", created_days_ago=30 - i)
    _mk_liveness(sessions_dir, "s1", days_ago=30)

    assert (await _sweep(db, sessions_dir, max_per_run=3))["created"] == 3
    assert (await _sweep(db, sessions_dir, max_per_run=3))["created"] == 3
    assert (await _sweep(db, sessions_dir, max_per_run=3))["created"] == 2
    assert (await _sweep(db, sessions_dir, max_per_run=3))["created"] == 0
    assert len(await _escalations(db)) == 8, "every eligible row escalates eventually"


async def test_deferred_counts_only_rows_not_yet_escalated(db, sessions_dir):
    """`deferred` must mean 'still owed', not 'seen this run' — otherwise the
    warning keeps naming rows that were escalated hours ago."""
    for i in range(5):
        await _mk_row(db, session_id="s1", text=f"item {i}", created_days_ago=30 - i)
    _mk_liveness(sessions_dir, "s1", days_ago=30)

    first = await _sweep(db, sessions_dir, max_per_run=2)
    assert first["created"] == 2 and first["deferred"] == 3
    second = await _sweep(db, sessions_dir, max_per_run=2)
    assert second["created"] == 2 and second["deferred"] == 1
    third = await _sweep(db, sessions_dir, max_per_run=2)
    assert third["created"] == 1 and third["deferred"] == 0


async def test_second_run_is_idempotent(db, sessions_dir):
    await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    assert (await _sweep(db, sessions_dir))["created"] == 1
    assert (await _sweep(db, sessions_dir))["created"] == 0
    assert len(await _escalations(db)) == 1


# ------------------------------------------------------------ follow-up shape


async def test_follow_up_shape_is_unclassified_and_asks_for_a_disposition(db, sessions_dir):
    row_id = await _mk_row(db, session_id="sess-xyz", text="build the widget", created_days_ago=30)
    _mk_liveness(sessions_dir, "sess-xyz", days_ago=30)
    await _sweep(db, sessions_dir)

    fu = (await _escalations(db))[0]
    assert fu["source"] == ESCALATION_SOURCE
    assert fu["strategy"] == "user_input_needed"
    assert fu["kind"] == "follow_up"
    assert fu["status"] == "pending"
    assert fu["priority"] == "high"
    assert fu["source_session"] == "sess-xyz"
    # UNCLASSIFIED: the sweep cannot tell a user errand from an internal item.
    assert fu["domain"] is None

    content = fu["content"]
    assert "build the widget" in content
    assert row_id in content, "the FULL id — session_ledger_update needs it"
    for exit_status in ("done", "absorbed", "dropped"):
        assert f"status='{exit_status}'" in content
    assert "charter.md" in content
    assert "WHICH LANE?" in content


async def test_the_sweep_never_writes_session_ledger(db, sessions_dir):
    """An evidence write would bump updated_at and read as a disposition the
    owner never made."""
    await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)

    async def snapshot():
        cur = await db.execute("SELECT * FROM session_ledger ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    before = await snapshot()
    await _sweep(db, sessions_dir)
    assert await snapshot() == before


async def test_dedup_key_matches_the_shared_helper(db, sessions_dir):
    """The link is the dedup_key; the two import-free hooks inline the same
    formula. A divergence here silently unlinks every row."""
    row_id = await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)
    assert (await _escalations(db))[0]["dedup_key"] == escalation_dedup_key(row_id)


# ------------------------------------------------------------- reverse sync


async def test_disposing_the_row_completes_its_follow_up(db, sessions_dir):
    row_id = await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)
    assert (await _escalations(db))[0]["status"] == "pending"

    await sc_crud.ledger_update(db, row_id, status="done", evidence="shipped in #123")

    result = await _sweep(db, sessions_dir)
    assert result["reconciled"] == 1
    fu = (await _escalations(db))[0]
    assert fu["status"] == "completed"
    assert "shipped in #123" in (fu["resolution_notes"] or "")


async def test_one_run_both_reconciles_and_escalates(db, sessions_dir):
    """Both halves do their work in a single run, under a cap that admits one
    creation.

    NOT an ordering test, deliberately. A disposed row is excluded from
    `ledger_stale_open` by its status filter, so the two passes operate on
    DISJOINT row sets and the order between them cannot change any result —
    verified by mutation (moving reconcile after the forward pass leaves every
    assertion green). The passes are ordered reconcile-first only so a run's log
    reads chronologically; nothing depends on it.
    """
    disposed = await _mk_row(db, session_id="s1", text="old", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir, max_per_run=1)
    await sc_crud.ledger_update(db, disposed, status="dropped", evidence="not needed")

    await _mk_row(db, session_id="s1", text="new", created_days_ago=29)
    result = await _sweep(db, sessions_dir, max_per_run=1)

    assert result["reconciled"] == 1
    assert result["created"] == 1


# --------------------------------------------------- the two dedup layers
# Idempotency is held by TWO independent mechanisms: our precheck, and the
# `idx_follow_ups_dedup` partial unique index. `test_second_run_is_idempotent`
# pins the OUTCOME and passes with either one alone (proved by mutation: the
# precheck can be deleted and it stays green). These two cells separate them.


async def test_the_precheck_short_circuits_before_the_insert(db, sessions_dir, caplog):
    """The precheck is what makes a re-run silent; the unique index is the
    backstop. If only the index were doing the work, the second run would emit
    a duplicate-key failure instead of skipping quietly."""
    await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)

    caplog.clear()
    with caplog.at_level("DEBUG", logger=esc.logger.name):
        result = await _sweep(db, sessions_dir)

    assert result["created"] == 0
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")], (
        "a re-run must skip via the precheck, not fall through to the index"
    )
    assert "already escalated by a concurrent sweep" not in caplog.text


async def test_a_lost_race_is_absorbed_quietly_not_logged_as_a_failure(
    db, sessions_dir, caplog, monkeypatch
):
    """Simulate the TOCTOU window: the precheck says 'absent', another sweep
    inserts first, our INSERT hits the unique index. That is the index working,
    not a broken sweep, so it must not produce an ERROR traceback."""
    await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)  # the "other sweep" wins

    async def _blind(*_a, **_k):
        return False

    monkeypatch.setattr(esc.fu_crud, "exists_by_dedup_key", _blind)

    caplog.clear()
    with caplog.at_level("DEBUG", logger=esc.logger.name):
        result = await _sweep(db, sessions_dir)

    assert result["created"] == 0
    assert len(await _escalations(db)) == 1
    assert "already escalated by a concurrent sweep" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "ERROR"], (
        "a lost race is the index doing its job — never an ERROR"
    )


async def test_reconcile_ignores_a_still_open_row(db, sessions_dir):
    await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)
    result = await _sweep(db, sessions_dir)
    assert result["reconciled"] == 0
    assert (await _escalations(db))[0]["status"] == "pending"


async def test_reconcile_leaves_other_sources_alone(db, sessions_dir):
    """A pending follow-up from another source must never be completed here."""
    other = await fu_crud.create(
        db,
        content="unrelated",
        source="inbox",
        strategy="user_input_needed",
        dedup_key="some-other-key",
    )
    row_id = await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)
    await sc_crud.ledger_update(db, row_id, status="done")
    await _sweep(db, sessions_dir)

    row = await fu_crud.get_by_id(db, other)
    assert row["status"] == "pending"


@pytest.mark.parametrize("status", ["in_progress", "scheduled", "blocked"])
async def test_reconcile_closes_escalations_a_human_picked_up(db, sessions_dir, status):
    """Every NON-TERMINAL status, not just 'pending'.

    Picking an escalation up moves it to in_progress. Reconciling only 'pending'
    would strand exactly the ones someone engaged with — and since the dedup key
    spans all statuses, nothing would ever re-create them.
    """
    row_id = await _mk_row(db, session_id="s1", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)
    fid = (await _escalations(db))[0]["id"]
    await db.execute("UPDATE follow_ups SET status=? WHERE id=?", (status, fid))
    await db.commit()

    await sc_crud.ledger_update(db, row_id, status="done", evidence="landed")

    assert (await _sweep(db, sessions_dir))["reconciled"] == 1
    assert (await _escalations(db))[0]["status"] == "completed"


async def test_a_failed_reconcile_is_reported_not_swallowed(db, sessions_dir, monkeypatch):
    """A reconcile that COULD NOT RUN and one with NOTHING TO DO both return
    zero. Without the flag the caller records success either way, so a
    permanently dead reverse sync hides behind a green job-health tile."""

    async def _boom(*_a, **_k):
        raise RuntimeError("ledger read exploded")

    monkeypatch.setattr(esc, "ledger_all", _boom)
    result = await _sweep(db, sessions_dir)
    assert result["reconcile_failed"] is True
    assert result["reconciled"] == 0


async def test_a_healthy_run_does_not_flag_reconcile_failure(db, sessions_dir):
    assert (await _sweep(db, sessions_dir))["reconcile_failed"] is False


# ------------------------------------------------------------------ egress


async def test_escalations_never_reach_the_morning_report(db, sessions_dir):
    """The privacy guarantee must hold STRUCTURALLY, not by domain accident.

    Follow-up content reproduces ledger row text verbatim and the report renders
    content[:200] into Telegram. Rows ship domain=None, which the report's
    exact-match user_world filter already excludes — but the follow-up's own body
    asks the reader to classify it, and choosing user_world would otherwise put
    unredacted ledger text on the wire. A guarantee that holds only until someone
    answers a question the feature itself poses is not a guarantee.
    """
    await _mk_row(db, session_id="s1", text="TOKEN-CANARY-xyz in the row", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)

    fid = (await _escalations(db))[0]["id"]
    # The user does exactly what the follow-up tells them to do.
    await db.execute("UPDATE follow_ups SET domain='user_world' WHERE id=?", (fid,))
    await db.commit()

    from genesis.outreach.morning_report import MorningReportGenerator

    # Call the REAL renderer. The first version of this test ran the CRUD query
    # itself and then filtered with a list comprehension IN THE TEST — so it
    # asserted its own filtering, never the report's, and stayed green with the
    # exclusion deleted from morning_report.py entirely. Verified by mutation.
    gen = MorningReportGenerator.__new__(MorningReportGenerator)
    gen._db = db
    rendered = await gen._get_follow_ups_summary() or ""
    assert "TOKEN-CANARY" not in rendered, (
        "verbatim ledger text reached the report that renders content[:200] to Telegram"
    )


@pytest.mark.parametrize("status", ["failed", "blocked"])
async def test_no_report_bucket_renders_an_escalation(db, sessions_dir, status):
    """EVERY bucket, not just the one that prompted the fix.

    `_get_follow_ups_summary` renders three lists from the same `content` field
    to the same Telegram message — needs-your-input, blocked/failed, and
    completed-24h. The first fix filtered only the first, so the leak had three
    doors and one was shut.
    """
    from genesis.outreach.morning_report import MorningReportGenerator

    await _mk_row(db, session_id="s1", text="TOKEN-CANARY-xyz", created_days_ago=30)
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir)
    fid = (await _escalations(db))[0]["id"]
    # The row is classified AND lands in the bucket under test.
    await db.execute(
        "UPDATE follow_ups SET domain='user_world', status=? WHERE id=?", (status, fid)
    )
    await db.commit()

    gen = MorningReportGenerator.__new__(MorningReportGenerator)
    gen._db = db
    assert "TOKEN-CANARY" not in (await gen._get_follow_ups_summary() or "")


async def test_reconcile_does_not_starve_older_rows_behind_completed_ones(
    db, sessions_dir, monkeypatch
):
    """The reverse pass must FILTER before it BOUNDS.

    `get_by_source` orders created_at DESC, so fetching by source and filtering
    non-terminal in Python puts the cap before the filter: once completed
    escalations outnumber the bound they fill it entirely and the older pending
    rows — the only ones reconcile exists to close — are never examined. Same
    class as the forward pass's starvation bug.
    """
    monkeypatch.setattr(esc, "_RECONCILE_READ_LIMIT", 3)
    rows = [await _mk_row(db, session_id="s1", text=f"i{i}", created_days_ago=30) for i in range(5)]
    _mk_liveness(sessions_dir, "s1", days_ago=30)
    await _sweep(db, sessions_dir, max_per_run=99)
    assert len(await _escalations(db)) == 5

    # Close every escalation EXCEPT the one belonging to rows[0]. Identify it by
    # dedup key, not by list position: all five ledger rows share a timestamp, so
    # the sweep's order does not track the order they were created in.
    keep = escalation_dedup_key(rows[0])
    for i, e in enumerate(await _escalations(db)):
        if e["dedup_key"] == keep:
            # Make the surviving PENDING row unambiguously the OLDEST. The five
            # follow-ups are created microseconds apart, and `get_by_source`
            # orders created_at DESC — without an explicit spread the row under
            # test lands inside the bound by luck and the test proves nothing
            # (measured: it stayed green with the filter back after the bound).
            await db.execute(
                "UPDATE follow_ups SET created_at=? WHERE id=?",
                ((NOW - timedelta(days=99)).isoformat(), e["id"]),
            )
        else:
            await db.execute(
                "UPDATE follow_ups SET created_at=? WHERE id=?",
                ((NOW - timedelta(days=i)).isoformat(), e["id"]),
            )
            await fu_crud.update_status(db, e["id"], "completed")
    await db.commit()
    await sc_crud.ledger_update(db, rows[0], status="done", evidence="landed")

    # With the cap before the filter, the 3-row bound is consumed by completed
    # rows and this reconcile finds nothing.
    assert (await _sweep(db, sessions_dir))["reconciled"] == 1


# ------------------------------------------------------------- empty state


async def test_a_fresh_install_is_silent(db, sessions_dir, tmp_path):
    """State zero: empty tables, and a sessions dir that does not exist."""
    absent = tmp_path / "no-such-dir"
    assert await _sweep(db, absent) == {
        "reconciled": 0,
        "reconcile_failed": False,
        "created": 0,
        "deferred": 0,
        "deferred_ids": [],
        "skipped_active": 0,
    }
