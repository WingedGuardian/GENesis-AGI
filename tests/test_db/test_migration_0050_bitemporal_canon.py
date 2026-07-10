"""Migration 0050 — bitemporal timestamp canonicalization data repair.

Seeds one row per live format class (2026-07-09 census) and verifies:
canonical + date-only untouched, Z/naive/space/offset canonicalized,
ranges → start date, month → first-of-month, free text → NULL, and
idempotence on re-run. Missing table is a safe no-op (bare-DB runner
lifecycle). Mirrors 0049's structure.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M50 = importlib.import_module(
    "genesis.db.migrations.0050_canonicalize_bitemporal_ts"
)

_DDL = """
    CREATE TABLE memory_metadata (
        memory_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        valid_at TEXT,
        invalid_at TEXT
    )
"""

_SEED = [
    # (memory_id, valid_at, invalid_at)
    ("m-canon", "2026-07-09T12:00:00+00:00", "2026-07-09T13:00:00+00:00"),
    ("m-micro", "2026-07-09T12:00:00.123456+00:00", None),
    ("m-date", "2026-06-14", None),
    ("m-z", "2026-05-03T17:30:26Z", None),
    ("m-naive", "2026-05-03T17:30:26", None),
    ("m-space", "2026-04-03 13:30:54", "2026-04-03 13:30:54"),
    ("m-offset", "2026-05-11T17:00:00-04:00", None),
    ("m-month", "2026-04", None),
    ("m-range-slash", "2026-03-18/2026-03-28", None),
    ("m-range-to", "2026-05-13 to 2026-05-18", None),
    ("m-garbage", "Friday", None),
    ("m-null", None, None),
]

_EXPECTED_VALID = {
    "m-canon": "2026-07-09T12:00:00+00:00",
    "m-micro": "2026-07-09T12:00:00.123456+00:00",
    "m-date": "2026-06-14",
    "m-z": "2026-05-03T17:30:26+00:00",
    "m-naive": "2026-05-03T17:30:26+00:00",
    "m-space": "2026-04-03T13:30:54+00:00",
    "m-offset": "2026-05-11T21:00:00+00:00",
    "m-month": "2026-04-01",
    "m-range-slash": "2026-03-18",
    "m-range-to": "2026-05-13",
    "m-garbage": None,
    "m-null": None,
}


async def _seeded_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(_DDL)
    for memory_id, valid_at, invalid_at in _SEED:
        await db.execute(
            "INSERT INTO memory_metadata "
            "(memory_id, created_at, valid_at, invalid_at) VALUES (?, ?, ?, ?)",
            (memory_id, "2026-07-09T00:00:00+00:00", valid_at, invalid_at),
        )
    return db


async def _column(db: aiosqlite.Connection, column: str) -> dict[str, str | None]:
    rows = await db.execute_fetchall(
        f"SELECT memory_id, {column} FROM memory_metadata"  # noqa: S608
    )
    return dict(rows)


@pytest.mark.asyncio
async def test_up_canonicalizes_every_format_class():
    db = await _seeded_db()
    try:
        await M50.up(db)
        assert await _column(db, "valid_at") == _EXPECTED_VALID
        invalid = await _column(db, "invalid_at")
        assert invalid["m-canon"] == "2026-07-09T13:00:00+00:00"
        assert invalid["m-space"] == "2026-04-03T13:30:54+00:00"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_is_idempotent():
    db = await _seeded_db()
    try:
        await M50.up(db)
        first = await _column(db, "valid_at")
        await M50.up(db)
        assert await _column(db, "valid_at") == first
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_missing_table_is_noop():
    db = await aiosqlite.connect(":memory:")
    try:
        await M50.up(db)  # must not raise
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sort_contract_after_repair():
    """Post-repair, the always-on filter's TEXT comparison is correct."""
    db = await _seeded_db()
    try:
        await M50.up(db)
        rows = await db.execute_fetchall(
            "SELECT memory_id FROM memory_metadata "
            "WHERE invalid_at IS NOT NULL AND invalid_at <= ?",
            ("2026-04-03T13:30:54+00:00",),
        )
        assert {r[0] for r in rows} == {"m-space"}
    finally:
        await db.close()
