"""Phase 1.5e — subsystem writes go SQLite + FTS5 only, skip Qdrant."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.memory.store import MemoryStore


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
            source_subsystem TEXT
        )
    """)
    await conn.execute("""
        CREATE VIRTUAL TABLE memory_fts USING fts5(
            memory_id, content, source_type, tags, collection
        )
    """)
    await conn.execute("""
        CREATE TABLE pending_embeddings (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            content TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            collection TEXT NOT NULL,
            created_at TEXT NOT NULL,
            tags TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT,
            confidence REAL,
            source_session_id TEXT,
            transcript_path TEXT,
            source_line_range TEXT,
            extraction_timestamp TEXT,
            source_pipeline TEXT,
            source_subsystem TEXT
        )
    """)
    await conn.commit()
    return conn


def _build_store(db) -> tuple[MemoryStore, MagicMock]:
    embed = MagicMock()
    embed.embed = AsyncMock(return_value=[0.1] * 1024)
    embed.tracker = MagicMock()
    qdrant = MagicMock()
    return MemoryStore(
        embedding_provider=embed, qdrant_client=qdrant, db=db,
    ), qdrant


@pytest.mark.asyncio
@patch("genesis.memory.store.upsert_point")
async def test_subsystem_write_skips_qdrant(mock_upsert, tmp_path) -> None:
    """`source_subsystem` set → no Qdrant upsert call, no pending row,
    metadata row gets `embedding_status='fts5_only'`."""
    db = await _build_db(str(tmp_path / "t.db"))
    try:
        store, _ = _build_store(db)
        mid = await store.store(
            content="reflection observation about a thing",
            source="deep_reflection",
            source_subsystem="reflection",
        )
        # No Qdrant upsert
        mock_upsert.assert_not_called()
        # memory_metadata row exists with fts5_only status + correct tag
        cursor = await db.execute(
            "SELECT embedding_status, source_subsystem FROM memory_metadata "
            "WHERE memory_id = ?", (mid,),
        )
        row = await cursor.fetchone()
        assert row == ("fts5_only", "reflection")
        # No pending_embeddings queue entry
        cursor = await db.execute(
            "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = ?",
            (mid,),
        )
        assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
@patch("genesis.memory.store.upsert_point")
async def test_user_write_still_hits_qdrant(mock_upsert, tmp_path) -> None:
    """First-hand (user-sourced) writes preserve the existing Qdrant path."""
    db = await _build_db(str(tmp_path / "t.db"))
    try:
        store, _ = _build_store(db)
        mid = await store.store(
            content="user said something interesting",
            source="user_message",
        )
        mock_upsert.assert_called_once()
        cursor = await db.execute(
            "SELECT embedding_status, source_subsystem FROM memory_metadata "
            "WHERE memory_id = ?", (mid,),
        )
        row = await cursor.fetchone()
        assert row == ("embedded", None)
    finally:
        await db.close()


@pytest.mark.asyncio
@patch("genesis.memory.store.upsert_point")
async def test_invalid_at_propagated(mock_upsert, tmp_path) -> None:
    """Caller-supplied `invalid_at` lands in `memory_metadata.invalid_at`."""
    db = await _build_db(str(tmp_path / "t.db"))
    try:
        store, _ = _build_store(db)
        mid = await store.store(
            content="reflection with TTL",
            source="deep_reflection",
            source_subsystem="reflection",
            invalid_at="2026-06-01T00:00:00+00:00",
        )
        cursor = await db.execute(
            "SELECT invalid_at FROM memory_metadata WHERE memory_id = ?",
            (mid,),
        )
        assert (await cursor.fetchone())[0] == "2026-06-01T00:00:00+00:00"
    finally:
        await db.close()
