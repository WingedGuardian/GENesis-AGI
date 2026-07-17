"""Migration 0064 — create ``ledger_predictions`` (WS-2 P1a substrate).

Verifies the full column set, the four indexes (including the partial
open-deadline index the grader rides), idempotency, the defense-in-depth
CHECK matrix, the dedupe UNIQUE key, fresh-canonical parity with
``_tables.py``, and ``down``.
"""

from __future__ import annotations

import importlib
import sqlite3

import aiosqlite
import pytest

M64 = importlib.import_module("genesis.db.migrations.0064_ledger_predictions")

_EXPECTED_COLUMNS = {
    "id",
    "created_at",
    "action_class",
    "subject_ref_type",
    "subject_ref_id",
    "domain",
    "metric",
    "comparator",
    "threshold",
    "confidence",
    "deadline_at",
    "provenance",
    "predictor",
    "source_session",
    "rationale",
    "status",
    "outcome_value",
    "resolved_at",
    "resolver",
    "evidence_ref",
    "brier",
    "metadata",
}

# Minimal valid row; tests override single fields to probe each CHECK.
_BASE_ROW = {
    "id": "p-1",
    "action_class": "outreach_send",
    "subject_ref_type": "outreach",
    "subject_ref_id": "o-1",
    "domain": "outreach.reminder",
    "metric": "reply_received",
    "comparator": "is_true",
    "threshold": None,
    "confidence": 0.5,
    "deadline_at": "2026-07-19T12:00:00+00:00",
    "provenance": "policy_prior",
    "predictor": "test",
}


async def _columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(ledger_predictions)")
    return {row[1] for row in await cur.fetchall()}


async def _insert(db: aiosqlite.Connection, **overrides) -> None:
    row = {**_BASE_ROW, **overrides}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    await db.execute(
        f"INSERT INTO ledger_predictions ({cols}) VALUES ({marks})",  # noqa: S608 — test-local column names
        tuple(row.values()),
    )


@pytest.mark.asyncio
async def test_up_creates_table_with_full_column_set(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M64.up(db)
        assert await _columns(db) == _EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_up_creates_indexes_incl_partial(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M64.up(db)
        cur = await db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='ledger_predictions' AND name LIKE 'idx_lp%'"
        )
        indexes = {row[0]: row[1] for row in await cur.fetchall()}
        assert set(indexes) == {
            "idx_lp_open_deadline",
            "idx_lp_domain",
            "idx_lp_status",
            "idx_lp_subject",
        }
        # the grader's hot query rides a PARTIAL index — open rows only
        assert "WHERE status = 'open'" in indexes["idx_lp_open_deadline"]


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M64.up(db)
        await M64.up(db)  # second run must not raise
        assert await _columns(db) == _EXPECTED_COLUMNS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"action_class": "bogus_class"},
        {"comparator": "eq"},
        {"confidence": 0.0},
        {"confidence": 1.0},
        # comparator/threshold pairing, both directions
        {"comparator": "le", "threshold": None},
        {"comparator": "is_true", "threshold": 5.0},
        {"provenance": "vibes"},
        {"status": "bogus"},
        {"outcome_value": 2},
        {"resolver": "oracle"},
    ],
)
async def test_check_matrix_rejects(tmp_path, overrides):
    """Defense-in-depth CHECKs hold even for raw-SQL writers."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M64.up(db)
        with pytest.raises(sqlite3.IntegrityError):
            await _insert(db, **overrides)


@pytest.mark.asyncio
async def test_unique_dedupe_key(tmp_path):
    """(action_class, subject_ref_id, metric) is idempotent under re-entry."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M64.up(db)
        await _insert(db)
        with pytest.raises(sqlite3.IntegrityError):
            await _insert(db, id="p-2")  # same subject + metric, different id
        # same subject, different metric is fine
        await _insert(db, id="p-3", metric="positive_engagement")


@pytest.mark.asyncio
async def test_fresh_canonical_parity(tmp_path):
    """_tables.py and the migration must build the identical column set."""
    from genesis.db.schema import TABLES

    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(TABLES["ledger_predictions"])
        fresh_cols = await _columns(db)
    async with aiosqlite.connect(str(tmp_path / "m.db")) as db:
        await M64.up(db)
        migrated_cols = await _columns(db)
    assert fresh_cols == migrated_cols == _EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_down_drops_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M64.up(db)
        await M64.down(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ledger_predictions'"
        )
        assert await cur.fetchone() is None
