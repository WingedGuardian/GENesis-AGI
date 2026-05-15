"""search_ranked must filter out rows past their invalid_at."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import memory as memory_crud


async def _build_db(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    await conn.execute("""
        CREATE TABLE memory_metadata (
            memory_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            collection TEXT NOT NULL DEFAULT 'episodic_memory',
            confidence REAL,
            embedding_status TEXT NOT NULL DEFAULT 'embedded',
            memory_class TEXT DEFAULT 'fact',
            wing TEXT,
            room TEXT,
            valid_at TEXT,
            invalid_at TEXT,
            source_subsystem TEXT,
            deprecated INTEGER NOT NULL DEFAULT 0,
            dream_cycle_run_id TEXT
        )
    """)
    await conn.execute("""
        CREATE VIRTUAL TABLE memory_fts USING fts5(
            memory_id, content, source_type, tags, collection
        )
    """)
    rows = [
        # (memory_id, invalid_at, content) — all contain "row" for FTS match
        ("alive",   None,                       "this row never expires"),
        ("future",  "2099-01-01T00:00:00+00:00", "this row is valid until 2099"),
        ("expired", "2020-01-01T00:00:00+00:00", "this row expired in 2020"),
    ]
    for mid, inv, content in rows:
        await conn.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, invalid_at) "
            "VALUES (?, ?, ?)", (mid, "2020-01-01T00:00:00+00:00", inv),
        )
        await conn.execute(
            "INSERT INTO memory_fts "
            "(memory_id, content, source_type, tags, collection) "
            "VALUES (?, ?, 'memory', '', 'episodic_memory')",
            (mid, content),
        )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_default_filters_expired(tmp_path) -> None:
    """Default as_of (now) drops 'expired', keeps 'alive' + 'future'."""
    conn = await _build_db(str(tmp_path / "t.db"))
    try:
        rows = await memory_crud.search_ranked(conn, query="row")
        ids = {r["memory_id"] for r in rows}
        assert ids == {"alive", "future"}
        assert "expired" not in ids
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_as_of_in_past_keeps_expired(tmp_path) -> None:
    """An explicit historical `as_of` returns rows valid at that point."""
    conn = await _build_db(str(tmp_path / "t.db"))
    try:
        rows = await memory_crud.search_ranked(
            conn, query="row", as_of="2019-01-01T00:00:00+00:00",
        )
        ids = {r["memory_id"] for r in rows}
        # At 2019, all three were valid (expired was valid until 2020-01-01)
        assert ids == {"alive", "future", "expired"}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_invalid_at_combines_with_subsystem_filter(tmp_path) -> None:
    """invalid_at + exclude_subsystems both apply on the same JOIN."""
    conn = await _build_db(str(tmp_path / "t.db"))
    try:
        # Tag 'future' as a subsystem write so it gets excluded too
        await conn.execute(
            "UPDATE memory_metadata SET source_subsystem='reflection' "
            "WHERE memory_id='future'"
        )
        await conn.commit()

        rows = await memory_crud.search_ranked(
            conn, query="row",
            exclude_subsystems=["ego", "triage", "reflection"],
        )
        ids = {r["memory_id"] for r in rows}
        # alive: NULL invalid_at + NULL subsystem → kept
        # future: NULL invalid_at fine, but reflection → excluded
        # expired: invalid_at past → excluded
        assert ids == {"alive"}
    finally:
        await conn.close()
