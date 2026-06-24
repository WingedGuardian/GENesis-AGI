"""Migration 0035 — backfill procedural_memory.invocation_count from history.

The reads signal lives in eval_events (procedure_invoked) for procedures
recalled before the invocation_count column was wired. This one-time migration
seeds the column from that history so the read signal doesn't start at zero.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import procedural

M35 = importlib.import_module(
    "genesis.db.migrations.0035_backfill_procedure_invocation_count"
)

_PROC = dict(
    principle="p", steps=["s"], tools_used=[], context_tags=[],
    created_at="2026-01-01T00:00:00",
)


async def _add_invoked_events(db, subject_id: str, n: int) -> None:
    for i in range(n):
        await db.execute(
            "INSERT INTO eval_events "
            "(id, timestamp, dimension, event_type, subject_id, metrics_json, created_at) "
            "VALUES (?, ?, 'procedure', 'procedure_invoked', ?, '{}', ?)",
            (f"{subject_id}-ev{i}", "2026-01-01T00:00:00", subject_id, "2026-01-01T00:00:00"),
        )
    await db.commit()


@pytest.fixture
async def db(tmp_path):
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _ic(db, pid: str) -> int:
    cur = await db.execute(
        "SELECT invocation_count FROM procedural_memory WHERE id = ?", (pid,)
    )
    return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_backfill_sets_count_from_events(db):
    await procedural.create(db, id="p-read", task_type="a", **_PROC)
    await procedural.create(db, id="p-many", task_type="b", **_PROC)
    await procedural.create(db, id="p-none", task_type="c", **_PROC)
    await _add_invoked_events(db, "p-read", 3)
    await _add_invoked_events(db, "p-many", 5)
    # A non-invoked event must not be counted.
    await db.execute(
        "INSERT INTO eval_events (id, timestamp, dimension, event_type, subject_id, "
        "metrics_json, created_at) VALUES "
        "('o1','2026-01-01T00:00:00','procedure','procedure_outcome','p-read','{}','2026-01-01T00:00:00')"
    )
    await db.commit()

    await M35.up(db)

    assert await _ic(db, "p-read") == 3
    assert await _ic(db, "p-many") == 5
    assert await _ic(db, "p-none") == 0  # untouched


@pytest.mark.asyncio
async def test_backfill_is_deterministic_on_rerun(db):
    await procedural.create(db, id="p1", task_type="a", **_PROC)
    await _add_invoked_events(db, "p1", 4)
    await M35.up(db)
    await M35.up(db)  # re-run = SET from events, same result
    assert await _ic(db, "p1") == 4


@pytest.mark.asyncio
async def test_backfill_ignores_orphan_events(db):
    # An event whose subject_id matches no procedure must not crash.
    await _add_invoked_events(db, "ghost", 2)
    await M35.up(db)  # no row to update; must complete cleanly
