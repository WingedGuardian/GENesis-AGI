"""Migration 0090 — WS-2 PR-5 calibration/prediction sunset.

Locks the properties of ``up()``/``down()``:

1. ``calibration_curves`` is dropped (dead-read legacy table).
2. ``predictions`` is archive-renamed to ``predictions_legacy_ws2`` with its rows
   preserved (locked decision: no hard drop).
3. Idempotency + build-path safety: the rename fires only when ``predictions``
   exists and the archive does not, so a re-run (or the fresh/isolation path where
   ``create_all_tables`` never created ``predictions``) is a clean no-op — never a
   "no such table" / "table already exists" failure, and never a fabricated archive.
4. The both-exist guard: if a live ``predictions`` and an ``predictions_legacy_ws2``
   both exist, ``up()`` must NOT clobber the live table.
5. ``down()`` restores ``calibration_curves`` and renames the archive back.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

_MIG = importlib.import_module("genesis.db.migrations.0090_ws2_calibration_sunset")


async def _tables(conn) -> list[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [r[0] for r in await cur.fetchall()]


async def _count(conn, table: str) -> int:
    cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - test table name
    return (await cur.fetchone())[0]


async def _seed_predictions(conn) -> None:
    """Create the legacy predictions table (minimal shape) with two rows."""
    await conn.execute(
        "CREATE TABLE predictions ("
        "id TEXT PRIMARY KEY, action_id TEXT, prediction TEXT, "
        "confidence REAL, confidence_bucket TEXT, domain TEXT, reasoning TEXT, "
        "outcome TEXT, correct INTEGER, matched_at TEXT)"
    )
    await conn.executemany(
        "INSERT INTO predictions (id, action_id, prediction, confidence, "
        "confidence_bucket, domain, reasoning) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("p1", "a1", "pred one", 0.8, "0.8-0.9", "outreach", "because"),
            ("p2", "a2", "pred two", 0.3, "0.3-0.4", "routing", "reasons"),
        ],
    )


@pytest.mark.asyncio
async def test_up_existing_install_drops_curves_and_archives_predictions(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        # Existing-install shape: both legacy tables present, predictions has rows.
        await _seed_predictions(conn)
        await conn.execute(
            "CREATE TABLE calibration_curves ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, confidence_bucket TEXT, "
            "predicted_confidence REAL, actual_success_rate REAL, sample_count INTEGER, "
            "correction_factor REAL)"
        )
        await conn.execute(
            "INSERT INTO calibration_curves (domain, confidence_bucket, predicted_confidence, "
            "actual_success_rate, sample_count, correction_factor) "
            "VALUES ('outreach', '0.8-0.9', 0.85, 0.7, 10, 0.82)"
        )

        await _MIG.up(conn)

        tables = await _tables(conn)
        assert "calibration_curves" not in tables  # dropped
        assert "predictions" not in tables  # renamed away
        assert "predictions_legacy_ws2" in tables  # archived
        assert await _count(conn, "predictions_legacy_ws2") == 2  # rows preserved

        # Re-run: clean no-op (rename guard skips; DROP IF EXISTS no-ops).
        await _MIG.up(conn)
        tables2 = await _tables(conn)
        assert "predictions_legacy_ws2" in tables2
        assert "predictions" not in tables2
        assert await _count(conn, "predictions_legacy_ws2") == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_up_fresh_install_is_a_noop(tmp_path):
    # Fresh/isolation shape (create_all_tables removed both from TABLES, so neither
    # exists when the runner reaches 0090): up() must not raise and must NOT fabricate
    # an archive — a fresh install never had legacy data to preserve.
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await _MIG.up(conn)  # must NOT raise
        tables = await _tables(conn)
        assert "predictions" not in tables
        assert "predictions_legacy_ws2" not in tables
        assert "calibration_curves" not in tables
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_up_does_not_clobber_when_archive_already_exists(tmp_path):
    # Defensive: a live predictions AND an existing archive → skip the rename so the
    # live table is never destroyed.
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await _seed_predictions(conn)
        await conn.execute("CREATE TABLE predictions_legacy_ws2 (id TEXT PRIMARY KEY)")

        await _MIG.up(conn)

        tables = await _tables(conn)
        assert "predictions" in tables  # untouched
        assert await _count(conn, "predictions") == 2
        assert "predictions_legacy_ws2" in tables
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_down_restores_curves_and_renames_back(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await _seed_predictions(conn)
        await _MIG.up(conn)
        assert "predictions_legacy_ws2" in await _tables(conn)

        await _MIG.down(conn)

        tables = await _tables(conn)
        assert "calibration_curves" in tables  # restored (empty)
        assert "predictions" in tables  # renamed back
        assert "predictions_legacy_ws2" not in tables
        assert await _count(conn, "predictions") == 2  # rows survived the round-trip
    finally:
        await conn.close()
