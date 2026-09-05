"""Tests for cc_sessions CRUD operations."""

import pytest

from genesis.db.crud import cc_sessions


@pytest.fixture
def sess_fields():
    return dict(
        id="sess-1",
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="user-1",
        channel="telegram",
        started_at="2026-03-07T08:00:00",
        last_activity_at="2026-03-07T08:00:00",
    )


async def test_create_and_get(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row is not None
    assert row["session_type"] == "foreground"
    assert row["model"] == "sonnet"


async def test_get_active_foreground(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    row = await cc_sessions.get_active_foreground(db, user_id="user-1", channel="telegram")
    assert row is not None
    assert row["id"] == "sess-1"


async def test_get_active_foreground_ignores_completed(db, sess_fields):
    await cc_sessions.create(db, **{**sess_fields, "status": "completed"})
    row = await cc_sessions.get_active_foreground(db, user_id="user-1", channel="telegram")
    assert row is None


async def test_update_status(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    ok = await cc_sessions.update_status(db, "sess-1", status="checkpointed")
    assert ok
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["status"] == "checkpointed"


async def test_update_activity(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    ok = await cc_sessions.update_activity(
        db,
        "sess-1",
        last_activity_at="2026-03-07T09:00:00",
    )
    assert ok
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["last_activity_at"] == "2026-03-07T09:00:00"


async def test_roster_persist_reconstruct_loop(db, sess_fields, monkeypatch):
    """Integration: the post-review resume-continuity loop end-to-end, hermetic
    (no live provider). Mirrors what the call sites do: persist the endpoint a
    routed run used (keyed off the roster NAME the chokepoint selected), then
    reconstruct overrides on resume, then confirm apply_active respects the
    pre-stamped routed resume. Closes the gap left when live GLM was unavailable."""
    import json

    from genesis.cc import roster
    from genesis.cc.types import CCInvocation

    monkeypatch.setenv("GENESIS_TEST_ROSTER_KEY", "sk-live")
    # Hermetic roster: the shipped config ships NO peers (install-specific, they
    # live in ~/.genesis/config/cc_roster.local.yaml), so inject one rather than
    # depend on whatever this machine happens to have configured.
    test_roster = {
        "default": "claude",
        "models": {
            "claude": {"native_subscription": True},
            "test-peer": {
                "anthropic_base_url": "https://example.invalid/api/anthropic",
                "model_id": "test-peer",
                "auth_env": "GENESIS_TEST_ROSTER_KEY",
            },
        },
    }
    # (persist) what conversation/direct_session compute from output.roster_model:
    payload = roster.endpoint_payload("test-peer", test_roster)
    assert payload and "token" not in payload  # NAME only, no secret
    await cc_sessions.create(db, **sess_fields)
    assert await cc_sessions.merge_metadata(db, "sess-1", {"roster_endpoint": payload})

    # (reconstruct) what _reconstruct_resume does on the next turn:
    row = await cc_sessions.get_by_id(db, "sess-1")
    ep = json.loads(row["metadata"])["roster_endpoint"]
    overrides = roster.overrides_from_persisted(ep)  # token re-read from env
    assert overrides["model_id_override"] == "test-peer"
    assert overrides["anthropic_auth_token"] == "sk-live"

    # (chokepoint) a pre-stamped routed resume is respected, never rerouted:
    inv = CCInvocation(
        prompt="x",
        roster_eligible=True,
        resume_session_id="cc-x",
        **overrides,
    )
    out_inv, name = roster.apply_active(inv)
    assert out_inv is inv and name == "test-peer"


async def test_merge_metadata_round_trip(db, sess_fields):
    # roster-endpoint persistence: shallow merge into JSON metadata, no migration.
    await cc_sessions.create(db, **{**sess_fields, "metadata": '{"existing": 1}'})
    ok = await cc_sessions.merge_metadata(
        db,
        "sess-1",
        {"roster_endpoint": {"model_id": "glm-5.2"}},
    )
    assert ok
    row = await cc_sessions.get_by_id(db, "sess-1")
    import json

    md = json.loads(row["metadata"])
    assert md["existing"] == 1  # preserved
    assert md["roster_endpoint"] == {"model_id": "glm-5.2"}  # merged


async def test_merge_metadata_tolerates_corrupt_and_missing(db, sess_fields):
    await cc_sessions.create(db, **{**sess_fields, "metadata": "not-json"})
    ok = await cc_sessions.merge_metadata(db, "sess-1", {"k": "v"})
    assert ok
    import json

    row = await cc_sessions.get_by_id(db, "sess-1")
    assert json.loads(row["metadata"]) == {"k": "v"}  # corrupt treated as {}
    # missing row → False, no raise
    assert await cc_sessions.merge_metadata(db, "nope", {"k": "v"}) is False


async def test_query_active(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    await cc_sessions.create(db, **{**sess_fields, "id": "sess-2", "status": "completed"})
    rows = await cc_sessions.query_active(db)
    assert len(rows) == 1


async def test_query_stale(db, sess_fields):
    # Background sessions should appear in stale results
    await cc_sessions.create(
        db,
        **{
            **sess_fields,
            "session_type": "background_task",
            "last_activity_at": "2026-03-07T06:00:00",
        },
    )
    rows = await cc_sessions.query_stale(db, older_than="2026-03-07T07:00:00")
    assert len(rows) == 1


async def test_query_stale_excludes_foreground(db, sess_fields):
    """Foreground sessions must never appear in stale query results."""
    await cc_sessions.create(
        db,
        **{**sess_fields, "session_type": "foreground", "last_activity_at": "2026-03-07T06:00:00"},
    )
    rows = await cc_sessions.query_stale(db, older_than="2026-03-07T07:00:00")
    assert len(rows) == 0


# The reap_stale crud tests were removed with the function itself —
# the stale-sweep policy (foreground preserved, background → 'expired',
# end-hooks fired) is covered at the SessionManager.cleanup_stale level in
# tests/test_cc/test_session_manager.py.


async def test_delete(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    assert await cc_sessions.delete(db, "sess-1")
    assert await cc_sessions.get_by_id(db, "sess-1") is None


async def test_check_constraint_session_type(db, sess_fields):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await cc_sessions.create(db, **{**sess_fields, "session_type": "invalid"})


async def test_check_constraint_status(db, sess_fields):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await cc_sessions.create(db, **{**sess_fields, "status": "invalid"})


@pytest.mark.asyncio
async def test_update_cc_session_id(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    ok = await cc_sessions.update_cc_session_id(db, "sess-1", cc_session_id="cc-cli-uuid-123")
    assert ok
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["cc_session_id"] == "cc-cli-uuid-123"


@pytest.mark.asyncio
async def test_cc_session_id_null_by_default(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["cc_session_id"] is None


@pytest.mark.asyncio
async def test_get_by_session_types(db, sess_fields):
    """Filters to the requested session_types; empty input returns []."""
    await cc_sessions.create(db, **sess_fields)  # foreground
    await cc_sessions.create(
        db, **{**sess_fields, "id": "sess-2", "session_type": "background_task"}
    )
    await cc_sessions.create(
        db, **{**sess_fields, "id": "sess-3", "session_type": "background_task"}
    )

    bg = await cc_sessions.get_by_session_types(db, {"background_task"})
    assert {r["id"] for r in bg} == {"sess-2", "sess-3"}

    both = await cc_sessions.get_by_session_types(db, {"background_task", "foreground"})
    assert {r["id"] for r in both} == {"sess-1", "sess-2", "sess-3"}

    assert await cc_sessions.get_by_session_types(db, set()) == []


@pytest.mark.asyncio
async def test_get_active_foreground_returns_checkpointed(db, sess_fields):
    """D3: a reaped (checkpointed) foreground row is still resumable — the
    widened query must return it (not just 'active')."""
    await cc_sessions.create(db, **{**sess_fields, "status": "checkpointed"})
    row = await cc_sessions.get_active_foreground(db, user_id="user-1", channel="telegram")
    assert row is not None and row["id"] == "sess-1"


@pytest.mark.asyncio
async def test_query_stale_foreground(db, sess_fields):
    """Selects idle foreground rows; excludes non-foreground, voice, and fresh."""
    old = "2020-01-01T00:00:00+00:00"
    await cc_sessions.create(db, **{**sess_fields, "id": "fg-old", "last_activity_at": old})
    await cc_sessions.create(
        db, **{**sess_fields, "id": "fg-fresh", "last_activity_at": "2099-01-01T00:00:00+00:00"}
    )
    await cc_sessions.create(
        db, **{**sess_fields, "id": "voice", "source_tag": "voice", "last_activity_at": old}
    )
    await cc_sessions.create(
        db,
        **{**sess_fields, "id": "bg", "session_type": "background_task", "last_activity_at": old},
    )
    rows = await cc_sessions.query_stale_foreground(db, older_than="2021-01-01T00:00:00+00:00")
    assert {r["id"] for r in rows} == {"fg-old"}


@pytest.mark.asyncio
async def test_checkpoint_dark_and_race_guard(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)  # active
    won = await cc_sessions.checkpoint_dark(
        db, "sess-1", checkpointed_at="2026-07-22T12:00:00+00:00"
    )
    assert won is True
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["status"] == "checkpointed"
    assert row["checkpointed_at"] == "2026-07-22T12:00:00+00:00"
    # Race guard: a second checkpoint on the now-non-active row is a no-op.
    again = await cc_sessions.checkpoint_dark(
        db, "sess-1", checkpointed_at="2026-07-22T13:00:00+00:00"
    )
    assert again is False


# ── Terminal-timestamp discipline (update_status as the single writer) ────
# Origin (measured 2026-09-04): completed_at had ZERO writers — 0 of 4556
# live rows ever carried one — because update_status wrote status alone and
# it is the sole status writer for complete/fail/expire.


async def test_update_status_terminal_stamps_completed_at(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    for status in ("completed", "failed", "expired"):
        await cc_sessions.update_status(
            db, "sess-1", status=status, ts="2026-03-07T09:00:00"
        )
        row = await cc_sessions.get_by_id(db, "sess-1")
        assert row["status"] == status
        assert row["completed_at"] == "2026-03-07T09:00:00", status


async def test_update_status_checkpoint_stamps_checkpointed_at(db, sess_fields):
    """SessionManager.checkpoint inherited the same omission — only the
    reaper's checkpoint_dark ever stamped checkpointed_at."""
    await cc_sessions.create(db, **sess_fields)
    await cc_sessions.update_status(
        db, "sess-1", status="checkpointed", ts="2026-03-07T09:00:00"
    )
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["checkpointed_at"] == "2026-03-07T09:00:00"
    assert row["completed_at"] is None


async def test_update_status_reopen_clears_completed_at(db, sess_fields):
    """A row flipped back to active must not keep a terminal timestamp —
    the pair (status='active', completed_at=<set>) is a lie in both
    directions. checkpointed_at stays as history, documented."""
    await cc_sessions.create(db, **sess_fields)
    await cc_sessions.update_status(
        db, "sess-1", status="completed", ts="2026-03-07T09:00:00"
    )
    await cc_sessions.update_status(db, "sess-1", status="active")
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["completed_at"] is None


async def test_update_status_default_ts_is_utc_now(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    await cc_sessions.update_status(db, "sess-1", status="completed")
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["completed_at"] is not None


# ── Honest filesystem adoption ────────────────────────────────────────────


async def test_register_from_filesystem_active_when_alive(db):
    """A live terminal session must adopt as ACTIVE — the measured lie was
    a 4-second-old session carrying status='completed'."""
    inserted = await cc_sessions.register_from_filesystem(
        db,
        id="cc-live",
        cc_session_id="cc-live",
        started_at="2026-03-07T08:00:00",
        status="active",
    )
    assert inserted
    row = await cc_sessions.get_by_id(db, "cc-live")
    assert row["status"] == "active"
    assert row["completed_at"] is None


async def test_register_from_filesystem_dead_stamps_completed_at(db):
    inserted = await cc_sessions.register_from_filesystem(
        db,
        id="cc-dead",
        cc_session_id="cc-dead",
        started_at="2026-03-07T08:00:00",
        status="completed",
        completed_at="2026-03-07T08:30:00",
    )
    assert inserted
    row = await cc_sessions.get_by_id(db, "cc-dead")
    assert row["status"] == "completed"
    assert row["completed_at"] == "2026-03-07T08:30:00"


# ── Dead-candidate query (pid-evidence fast path) ─────────────────────────


async def test_query_dead_candidate_foreground(db, sess_fields):
    """Terminal-registered rows only: pid set AND id == cc_session_id;
    channel rows (uuid4 id ≠ cc uuid) and voice rows excluded."""
    # Terminal-registered, idle, pid known → candidate.
    await cc_sessions.register_from_filesystem(
        db, id="term-1", cc_session_id="term-1",
        started_at="2026-03-07T00:00:00", status="active",
    )
    await cc_sessions.set_pid(db, "term-1", pid=4242)
    # Channel row: pid set but id != cc_session_id → never a candidate.
    ch = dict(sess_fields, id="chan-1", started_at="2026-03-07T00:00:00",
              last_activity_at="2026-03-07T00:00:00")
    await cc_sessions.create(db, **ch)
    await cc_sessions.update_cc_session_id(db, "chan-1", cc_session_id="other-uuid")
    await cc_sessions.set_pid(db, "chan-1", pid=4243)
    # Voice row: id == cc_session_id but excluded by tag.
    await cc_sessions.register_voice_session(
        db, id="voice-1", started_at="2026-03-07T00:00:00",
    )
    await cc_sessions.set_pid(db, "voice-1", pid=4244)

    rows = await cc_sessions.query_dead_candidate_foreground(
        db, older_than="2026-03-07T01:00:00"
    )
    assert [r["id"] for r in rows] == ["term-1"]
    assert rows[0]["pid"] == 4242


async def test_voice_orphan_sweep_stamps_completed_at(db):
    await cc_sessions.register_voice_session(
        db, id="voice-2", started_at="2026-03-07T00:00:00",
    )
    n = await cc_sessions.complete_orphaned_voice_sessions(
        db, idle_before="2026-03-07T01:00:00"
    )
    assert n == 1
    row = await cc_sessions.get_by_id(db, "voice-2")
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
