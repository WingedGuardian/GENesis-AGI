"""Migration 0068 — create ``calibration_cells`` + ``calibration_cell_history``.

Verifies the full column set of both tables, the history trend index,
idempotency, the CHECK matrix on ``calibration_cells`` (the history table
carries no CHECKs by design — it is written only by the grader from
already-validated cells), the composite-PK dedupe, fresh-canonical parity
with ``_tables.py``, and ``down``.
"""

from __future__ import annotations

import importlib
import sqlite3

import aiosqlite
import pytest

M68 = importlib.import_module("genesis.db.migrations.0068_calibration_cells")

_EXPECTED_CELL_COLUMNS = {
    "domain",
    "action_class",
    "metric",
    "provenance",
    "window_days",
    "n",
    "n_mechanical",
    "base_rate",
    "mean_confidence",
    "brier",
    "reliability",
    "resolution",
    "uncertainty",
    "ece",
    "shrunk_estimate",
    "status",
    "computed_at",
}

_EXPECTED_HISTORY_COLUMNS = {
    "id",
    "domain",
    "action_class",
    "metric",
    "provenance",
    "window_days",
    "n",
    "brier",
    "reliability",
    "resolution",
    "ece",
    "status",
    "snapshot_at",
}

# Minimal valid cell row; tests override single fields to probe each CHECK.
_BASE_CELL = {
    "domain": "outreach.general",
    "action_class": "outreach_send",
    "metric": "reply_received",
    "provenance": "stated",
    "window_days": 90,
    "n": 40,
    "n_mechanical": 40,
    "base_rate": 0.5,
    "mean_confidence": 0.6,
    "status": "ok",
    "computed_at": "2026-07-19T12:00:00+00:00",
}


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608 — test-local table names
    return {row[1] for row in await cur.fetchall()}


async def _insert_cell(db: aiosqlite.Connection, **overrides) -> None:
    row = {**_BASE_CELL, **overrides}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    await db.execute(
        f"INSERT INTO calibration_cells ({cols}) VALUES ({marks})",  # noqa: S608 — test-local column names
        tuple(row.values()),
    )


@pytest.mark.asyncio
async def test_up_creates_both_tables_with_full_column_sets(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M68.up(db)
        assert await _columns(db, "calibration_cells") == _EXPECTED_CELL_COLUMNS
        assert await _columns(db, "calibration_cell_history") == _EXPECTED_HISTORY_COLUMNS


@pytest.mark.asyncio
async def test_up_creates_history_trend_index(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M68.up(db)
        cur = await db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='calibration_cell_history' AND name='idx_cch_cell_time'"
        )
        row = await cur.fetchone()
        assert row is not None
        # trend queries ride (domain, metric, snapshot_at)
        assert "domain, metric, snapshot_at" in row[1]


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M68.up(db)
        await M68.up(db)  # second run must not raise
        assert await _columns(db, "calibration_cells") == _EXPECTED_CELL_COLUMNS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"provenance": "vibes"},
        {"status": "bogus"},
        {"n": None},
        {"computed_at": None},
    ],
)
async def test_check_matrix_rejects(tmp_path, overrides):
    """Defense-in-depth CHECKs hold even for raw-SQL writers."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M68.up(db)
        with pytest.raises(sqlite3.IntegrityError):
            await _insert_cell(db, **overrides)


@pytest.mark.asyncio
async def test_composite_pk_dedupes(tmp_path):
    """(domain, action_class, metric, provenance, window_days) is the cell key."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M68.up(db)
        await _insert_cell(db)
        with pytest.raises(sqlite3.IntegrityError):
            await _insert_cell(db, n=99)  # same key, different payload
        # a different window is a different cell
        await _insert_cell(db, window_days=30)


@pytest.mark.asyncio
async def test_fresh_canonical_parity(tmp_path):
    """_tables.py and the migration must build identical column sets."""
    from genesis.db.schema import TABLES

    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(TABLES["calibration_cells"])
        await db.execute(TABLES["calibration_cell_history"])
        fresh_cells = await _columns(db, "calibration_cells")
        fresh_history = await _columns(db, "calibration_cell_history")
    async with aiosqlite.connect(str(tmp_path / "m.db")) as db:
        await M68.up(db)
        migrated_cells = await _columns(db, "calibration_cells")
        migrated_history = await _columns(db, "calibration_cell_history")
    assert fresh_cells == migrated_cells == _EXPECTED_CELL_COLUMNS
    assert fresh_history == migrated_history == _EXPECTED_HISTORY_COLUMNS


@pytest.mark.asyncio
async def test_down_drops_both_tables(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M68.up(db)
        await M68.down(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('calibration_cells','calibration_cell_history')"
        )
        assert await cur.fetchall() == []
