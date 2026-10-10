"""Migration: explicit supersessions recorded before ``mark_superseded`` cleared
the dream run id must stop reading as dream retirements."""

from __future__ import annotations

import importlib

import pytest

_MIG = "genesis.db.migrations.20261010025108_clear_dream_run_on_explicit_supersessions"

# (memory_id, dream_cycle_run_id, superseded_by, run id expected afterwards)
_ROWS = [
    # dream-retired, then explicitly superseded by the old mark_superseded
    ("retired-then-superseded", "run-x", "explicit-1", None),
    # a synthesis explicitly superseded by the old mark_superseded
    ("synthesis-superseded", "synthesis:run-x", "explicit-2", None),
    # an ordinary dream retirement written before the change (no pointer)
    ("dream-only", "run-x", None, "run-x"),
    ("synthesis-only", "synthesis:run-x", None, "synthesis:run-x"),
    # an ordinary explicit supersession
    ("explicit-only", None, "explicit-3", None),
]


@pytest.mark.asyncio
async def test_clears_the_run_id_only_where_an_explicit_pointer_exists(db):
    for mid, run_id, succ, _ in _ROWS:
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, deprecated, "
            "dream_cycle_run_id, superseded_by) VALUES (?, '2026-01-01', 1, ?, ?)",
            (mid, run_id, succ),
        )
    await db.commit()
    mig = importlib.import_module(_MIG)

    for _ in range(2):  # idempotent
        await mig.up(db)
        for mid, _run, succ, expected in _ROWS:
            cur = await db.execute(
                "SELECT dream_cycle_run_id, superseded_by, deprecated "
                "FROM memory_metadata WHERE memory_id = ?",
                (mid,),
            )
            row = dict(await cur.fetchone())
            assert row == {
                "dream_cycle_run_id": expected,
                "superseded_by": succ,
                "deprecated": 1,
            }, mid


@pytest.mark.asyncio
async def test_no_memory_table_is_a_no_op(tmp_path):
    import aiosqlite

    async with aiosqlite.connect(tmp_path / "empty.db") as conn:
        await importlib.import_module(_MIG).up(conn)
