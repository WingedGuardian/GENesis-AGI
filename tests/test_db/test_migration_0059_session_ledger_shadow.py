"""Migration 0059 + CRUD — session_ledger_shadow runs/events (PR-3 SHADOW store).

Verifies tables/columns/indexes/idempotency/down, the subprocess
table-existence guard (pre-migration no-op, cursor-preserving), the
run+events single-transaction write, validation, and retention pruning.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import session_ledger_shadow as crud

M59 = importlib.import_module("genesis.db.migrations.0059_session_ledger_shadow")

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return await cur.fetchone() is not None


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        # the guard caches TRUE per-process — reset between tests
        crud._tables_verified = False
        yield conn
        crud._tables_verified = False


def _run_kwargs(**over):
    kw = dict(
        run_id="r1",
        session_id=SID,
        started_at="2026-07-14T12:00:00+00:00",
        finished_at="2026-07-14T12:00:20+00:00",
        start_byte=0,
        end_byte=5000,
        trigger="manual",
        status="ok",
        n_user_turns=3,
        n_proposals=1,
        latency_ms=12000,
        prompt_version="v1",
        model="claude-haiku-4-5-20251001",
    )
    kw.update(over)
    return kw


def _event(**over):
    ev = dict(
        id="e1",
        observed_at="2026-07-14T12:00:20+00:00",
        kind="agreement",
        text="ship the rollback lever with the widget refactor",
        turn_ref="u-123",
        quote_preview="yes, do that",
        quote_hash="abc",
        quote_verified=True,
        match_kind="none",
    )
    ev.update(over)
    return ev


# ── migration shape ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_creates_tables_with_expected_columns(db):
    await M59.up(db)
    assert await _columns(db, "session_ledger_shadow_runs") == {
        "run_id",
        "session_id",
        "started_at",
        "finished_at",
        "start_byte",
        "end_byte",
        "trigger",
        "status",
        "truncated",
        "n_user_turns",
        "n_proposals",
        "latency_ms",
        "prompt_version",
        "model",
        "mode",
        "detail",
    }
    assert await _columns(db, "session_ledger_shadow_events") == {
        "id",
        "run_id",
        "observed_at",
        "session_id",
        "kind",
        "text",
        "turn_ref",
        "quote_preview",
        "quote_hash",
        "quote_verified",
        "match_kind",
        "matched_item_id",
        "match_score",
        "duplicate_of",
        "mode",
    }


@pytest.mark.asyncio
async def test_up_idempotent_and_down(db):
    await M59.up(db)
    await M59.up(db)  # IF NOT EXISTS — no raise
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_sls%'"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert names == {
        "idx_slsr_session",
        "idx_slsr_started",
        "idx_slse_session",
        "idx_slse_observed",
    }
    await M59.down(db)
    assert not await _table_exists(db, "session_ledger_shadow_runs")
    assert not await _table_exists(db, "session_ledger_shadow_events")


@pytest.mark.asyncio
async def test_status_and_kind_checks_enforced(db):
    await M59.up(db)
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO session_ledger_shadow_runs "
            "(run_id, session_id, started_at, start_byte, end_byte, trigger, status) "
            "VALUES ('r', 's', 't', 0, 1, 'manual', 'bogus')"
        )
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO session_ledger_shadow_events "
            "(id, run_id, observed_at, session_id, kind, text) "
            "VALUES ('e', 'r', 't', 's', 'bogus', 'x')"
        )


# ── CRUD ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_run_with_events_roundtrip(db):
    await M59.up(db)
    ok = await crud.record_run(db, **_run_kwargs(), events=[_event()])
    assert ok is True
    runs = await crud.list_runs(db, SID)
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["mode"] == "shadow"
    events = await crud.list_events(db, SID)
    assert len(events) == 1
    ev = events[0]
    assert ev["run_id"] == "r1"
    assert ev["session_id"] == SID
    assert ev["quote_verified"] == 1
    assert ev["mode"] == "shadow"


@pytest.mark.asyncio
async def test_record_run_zero_events_still_recorded(db):
    """A successful empty run must exist — it charges its window's missed
    foreground rows as FNs in the report."""
    await M59.up(db)
    assert await crud.record_run(db, **_run_kwargs(status="empty_delta", n_proposals=0))
    runs = await crud.list_runs(db)
    assert runs[0]["status"] == "empty_delta"
    assert await crud.list_events(db) == []


@pytest.mark.asyncio
async def test_pre_migration_guard_noops(db):
    """Subprocess writer against an un-migrated DB: no-op, no create, and
    the caller can see False (so it must NOT advance its cursor)."""
    assert await crud.record_run(db, **_run_kwargs()) is False
    assert not await _table_exists(db, "session_ledger_shadow_runs")
    # self-heal: once the migration lands the same process writes fine
    await M59.up(db)
    assert await crud.record_run(db, **_run_kwargs()) is True


@pytest.mark.asyncio
async def test_validation_rejects_bad_values(db):
    await M59.up(db)
    with pytest.raises(ValueError):
        await crud.record_run(db, **_run_kwargs(status="bogus"))
    with pytest.raises(ValueError):
        await crud.record_run(db, **_run_kwargs(), events=[_event(kind="bogus")])
    with pytest.raises(ValueError):
        await crud.record_run(db, **_run_kwargs(), events=[_event(match_kind="bogus")])


@pytest.mark.asyncio
async def test_summary_counts(db):
    await M59.up(db)
    await crud.record_run(db, **_run_kwargs(run_id="r1"), events=[_event(id="e1")])
    await crud.record_run(
        db,
        **_run_kwargs(run_id="r2", status="failed", detail="exit_3"),
    )
    await crud.record_run(
        db,
        **_run_kwargs(run_id="r3"),
        events=[_event(id="e2", kind="pivot", match_kind="fuzzy", match_score=0.9)],
    )
    s = await crud.summary(db)
    assert s["runs"] == {"ok": 2, "failed": 1}
    assert s["events"] == {"agreement/none": 1, "pivot/fuzzy": 1}


@pytest.mark.asyncio
async def test_prune_deletes_old_runs_and_events(db):
    await M59.up(db)
    await crud.record_run(
        db,
        **_run_kwargs(run_id="old", started_at="2026-05-01T00:00:00+00:00"),
        events=[_event(id="e-old", observed_at="2026-05-01T00:00:10+00:00")],
    )
    await crud.record_run(
        db,
        **_run_kwargs(run_id="new", started_at="2026-07-10T00:00:00+00:00"),
        events=[_event(id="e-new", observed_at="2026-07-10T00:00:10+00:00")],
    )
    deleted = await crud.prune_session_ledger_shadow(
        db,
        older_than_days=45,
        now="2026-07-14T00:00:00+00:00",
    )
    assert deleted == 2
    assert [r["run_id"] for r in await crud.list_runs(db)] == ["new"]
    assert [e["id"] for e in await crud.list_events(db)] == ["e-new"]


@pytest.mark.asyncio
async def test_prune_noops_pre_migration(db):
    assert await crud.prune_session_ledger_shadow(db, now="2026-07-14T00:00:00+00:00") == 0
