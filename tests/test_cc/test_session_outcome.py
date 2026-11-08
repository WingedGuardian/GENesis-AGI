"""How a session ENDED is recorded with its terminal status (stopped-session paging, PR 1).

Before this, ``cc_sessions`` recorded only THAT a session ended: ``fail()``
discarded its reason, a truncated run read ``completed``, and four writers ended
sessions outside ``SessionManager`` entirely. The outcome now rides the single
status writer, ``crud.cc_sessions.update_status``, so every writer is covered.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.cc.session_manager import SessionManager
from genesis.db.crud import cc_sessions
from genesis.db.schema import create_all_tables

_MIG = importlib.import_module("genesis.db.migrations.20261104144136_cc_sessions_outcome")
_NEW = ("outcome", "outcome_reason", "outcome_paged_at")


async def _row(db, sid: str) -> dict:
    return await cc_sessions.get_by_id(db, sid)


async def _make(db, sid: str, **kw) -> None:
    base = dict(
        id=sid,
        session_type="background_task",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="u1",
        channel="bridge",
        started_at="2026-03-07T06:00:00",
        last_activity_at="2026-03-07T06:00:00",
        source_tag="campaign",
    )
    base.update(kw)
    await cc_sessions.create(db, **base)


@pytest.fixture
async def manager(db):
    return SessionManager(db=db, day_boundary_hour=0)


# ── the writer ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [("completed", "unreported"), ("failed", "failed"), ("expired", "expired")],
)
async def test_a_terminal_status_is_never_left_unjudged(db, status, expected):
    """A writer that passes no outcome (the bypass writers) still records one."""
    await _make(db, "s")
    await cc_sessions.update_status(db, "s", status=status)
    row = await _row(db, "s")
    assert (row["status"], row["outcome"], row["outcome_reason"]) == (status, expected, None)


async def test_an_explicit_outcome_and_reason_land_in_the_same_write(db):
    await _make(db, "s")
    await cc_sessions.update_status(
        db, "s", status="completed", outcome="partial", outcome_reason="bg_truncated"
    )
    row = await _row(db, "s")
    assert (row["outcome"], row["outcome_reason"]) == ("partial", "bg_truncated")
    assert row["completed_at"]


async def test_an_unknown_outcome_never_vetoes_the_status_write(db):
    """The outcome only adds information. A bad value is logged and replaced by
    the status default; raising would leave the row active, and two callers
    retry status writes in a loop."""
    await _make(db, "s")
    assert await cc_sessions.update_status(db, "s", status="failed", outcome="oops")
    row = await _row(db, "s")
    assert (row["status"], row["outcome"]) == ("failed", "failed")
    assert row["outcome_reason"] == "invalid_outcome:oops"  # findable from the DB


async def test_only_if_active_holds_on_every_status(db):
    await _make(db, "s", status="completed")
    for status in ("completed", "failed", "expired", "checkpointed", "active"):
        assert not await cc_sessions.update_status(db, "s", status=status, only_if_active=True)
    assert (await _row(db, "s"))["status"] == "completed"


async def test_reopening_clears_the_outcome_so_the_next_end_is_judged_afresh(db):
    await _make(db, "s")
    await cc_sessions.update_status(db, "s", status="failed", outcome_reason="boom")
    await db.execute("UPDATE cc_sessions SET outcome_paged_at = 'x' WHERE id = 's'")
    await cc_sessions.update_status(db, "s", status="active")
    row = await _row(db, "s")
    assert [row[c] for c in _NEW] == [None, None, None]


async def test_a_late_finish_overwrites_the_outcome_but_not_the_page_stamp(db):
    """Expired, reported, then the real result arrives: the result wins, and
    the stop that was already reported is not reported again."""
    await _make(db, "s")
    await cc_sessions.update_status(db, "s", status="expired", outcome="expired")
    await db.execute("UPDATE cc_sessions SET outcome_paged_at = 'paged' WHERE id = 's'")
    await cc_sessions.update_status(db, "s", status="completed", outcome="finished")
    row = await _row(db, "s")
    assert (row["outcome"], row["outcome_paged_at"]) == ("finished", "paged")


async def test_checkpoint_records_no_outcome(db):
    await _make(db, "s")
    await cc_sessions.update_status(db, "s", status="checkpointed")
    assert (await _row(db, "s"))["outcome"] is None


# ── SessionManager ─────────────────────────────────────────────────────────


async def test_fail_keeps_its_reason(db, manager):
    """The reason used to be discarded (cancel, exception text, inbox error)."""
    await _make(db, "s")
    await manager.fail("s", reason="cancelled")
    row = await _row(db, "s")
    assert (row["status"], row["outcome"], row["outcome_reason"]) == (
        "failed",
        "failed",
        "cancelled",
    )


async def test_a_rate_limit_park_is_parked_not_failed(db, manager):
    await _make(db, "s")
    await manager.fail("s", reason="rate_limited_parked", outcome="parked")
    row = await _row(db, "s")
    assert (row["status"], row["outcome"]) == ("failed", "parked")


async def test_complete_passes_the_judged_outcome_through(db, manager):
    await _make(db, "a")
    await _make(db, "b")
    await manager.complete("a", outcome="partial", outcome_reason="no_result_event")
    await manager.complete("b")
    assert (await _row(db, "a"))["outcome"] == "partial"
    assert (await _row(db, "b"))["outcome"] == "unreported"


async def test_the_idle_reaper_records_an_expiry(db, manager):
    await _make(db, "s")
    assert await manager.cleanup_stale(max_idle_minutes=60) == 1
    row = await _row(db, "s")
    assert (row["outcome"], row["outcome_reason"]) == ("expired", "expired_idle")


async def test_the_reaper_never_stamps_a_session_that_finished_meanwhile(
    db, manager, monkeypatch
):
    """Read-then-write race: the row completed after query_stale read it."""
    await _make(db, "s")
    real = cc_sessions.query_stale

    async def read_then_finish(conn, *, older_than):
        rows = await real(conn, older_than=older_than)
        await cc_sessions.update_status(conn, "s", status="completed", outcome="finished")
        return rows

    hooks: list = []

    async def hook(sid):
        hooks.append(sid)

    manager.add_on_end(hook)
    monkeypatch.setattr(cc_sessions, "query_stale", read_then_finish)
    assert await manager.cleanup_stale(max_idle_minutes=60) == 0
    row = await _row(db, "s")
    assert (row["status"], row["outcome"]) == ("completed", "finished")
    assert hooks == []


async def test_the_outcome_is_visible_to_end_hooks(db, manager):
    """The ego follow-through hook runs inside the end path; it must see it."""
    seen: list = []

    async def hook(sid):
        seen.append((await _row(db, sid))["outcome_reason"])

    manager.add_on_end(hook)
    await _make(db, "s")
    await manager.fail("s", reason="CC invocation error")
    assert seen == ["CC invocation error"]


# ── schema, both build paths ───────────────────────────────────────────────

_LEGACY = """
    CREATE TABLE cc_sessions (
        id TEXT PRIMARY KEY, session_type TEXT NOT NULL, user_id TEXT, channel TEXT,
        model TEXT NOT NULL, effort TEXT NOT NULL DEFAULT 'medium',
        status TEXT NOT NULL DEFAULT 'active', pid INTEGER, started_at TEXT NOT NULL,
        last_activity_at TEXT NOT NULL, checkpointed_at TEXT, completed_at TEXT,
        source_tag TEXT NOT NULL DEFAULT 'foreground', metadata TEXT
    )
"""


async def _cols(conn) -> list[str]:
    cur = await conn.execute("PRAGMA table_info(cc_sessions)")
    return [r[1] for r in await cur.fetchall()]


async def test_migration_adds_the_columns_idempotently_without_backfill(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await conn.execute(_LEGACY)
        await conn.execute(
            "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at,"
            " status) VALUES ('old', 'background_task', 'sonnet', 'x', 'x', 'failed')"
        )
        await _MIG.up(conn)
        cols = await _cols(conn)
        assert set(_NEW) <= set(cols)
        cur = await conn.execute(
            "SELECT outcome, outcome_reason, outcome_paged_at FROM cc_sessions"
        )
        assert await cur.fetchone() == (None, None, None)  # NULL = not judged, never paged
        await _MIG.up(conn)
        assert await _cols(conn) == cols
    finally:
        await conn.close()


async def test_migration_is_a_noop_without_the_table():
    conn = await aiosqlite.connect(":memory:")
    try:
        await _MIG.up(conn)
    finally:
        await conn.close()


async def test_the_base_path_upgrades_an_existing_table(tmp_path):
    """create_all_tables on an EXISTING install must add the columns itself
    (schema_both_build_paths), not leave them to the numbered migration."""
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await conn.execute(_LEGACY)
        await create_all_tables(conn)
        assert set(_NEW) <= set(await _cols(conn))
    finally:
        await conn.close()
