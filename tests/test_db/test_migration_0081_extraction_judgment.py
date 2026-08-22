"""MW-1 — 0081 extraction-judgment columns: migration + persistence.

Covers the STORE side: the five judgment columns exist on both schema build
paths (numbered migration for legacy DBs, canonical CREATE for fresh DBs), the
migration is idempotent, and ``create_metadata`` persists the axes to
``memory_metadata``.
"""

from __future__ import annotations

import importlib

import aiosqlite

from genesis.db.crud import memory
from genesis.db.schema import TABLES

_mig = importlib.import_module("genesis.db.migrations.0081_mw1_extraction_judgment")

_JUDGMENT_COLS = {
    "speech_act",
    "speech_act_confidence",
    "assertion_provenance",
    "durability",
    "expires_at",
}

# A memory_metadata shaped like a legacy DB that predates MW-1 (no judgment cols).
_LEGACY_DDL = """
    CREATE TABLE memory_metadata (
        memory_id        TEXT PRIMARY KEY,
        created_at       TEXT NOT NULL,
        collection       TEXT NOT NULL DEFAULT 'episodic_memory',
        confidence       REAL,
        embedding_status TEXT NOT NULL DEFAULT 'embedded',
        memory_class     TEXT DEFAULT 'fact',
        valid_at         TEXT,
        invalid_at       TEXT,
        origin_class     TEXT
    )
"""


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_migration_adds_all_five_columns_to_legacy_db():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_LEGACY_DDL)
        await conn.commit()
        assert _JUDGMENT_COLS & await _columns(conn, "memory_metadata") == set()

        await _mig.up(conn)
        await conn.commit()

        cols = await _columns(conn, "memory_metadata")
        assert cols >= _JUDGMENT_COLS
    finally:
        await conn.close()


async def test_migration_is_idempotent():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_LEGACY_DDL)
        await conn.commit()
        await _mig.up(conn)
        await _mig.up(conn)  # second run must not raise (duplicate-column guard)
        await conn.commit()
        assert await _columns(conn, "memory_metadata") >= _JUDGMENT_COLS
    finally:
        await conn.close()


async def test_fresh_db_create_carries_columns():
    """The canonical CREATE TABLE (fresh-DB path) must already carry the cols —
    else a fresh install diverges from a migrated one (base-path parity)."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_metadata"])
        await conn.commit()
        assert await _columns(conn, "memory_metadata") >= _JUDGMENT_COLS
    finally:
        await conn.close()


async def test_create_metadata_persists_judgment_fields():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_metadata"])
        await conn.commit()

        await memory.create_metadata(
            conn,
            memory_id="mw1-a",
            created_at="2026-08-06T00:00:00+00:00",
            speech_act="rule",
            speech_act_confidence=0.95,
            assertion_provenance="user",
            durability="temporary",
            expires_at="2026-08-07",
        )

        cur = await conn.execute(
            "SELECT speech_act, speech_act_confidence, assertion_provenance, "
            "durability, expires_at FROM memory_metadata WHERE memory_id='mw1-a'"
        )
        row = await cur.fetchone()
        assert row[0] == "rule"
        assert row[1] == 0.95
        assert row[2] == "user"
        assert row[3] == "temporary"
        # expires_at is canonicalized to full ISO by create_metadata.
        assert row[4] is not None and row[4].startswith("2026-08-07")
    finally:
        await conn.close()


async def test_create_metadata_defaults_null_when_unclassified():
    """Non-extraction / unclassified writes leave the axes NULL (never a false
    'temporary' — expiry is opt-in)."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_metadata"])
        await conn.commit()

        await memory.create_metadata(
            conn, memory_id="mw1-b", created_at="2026-08-06T00:00:00+00:00"
        )

        cur = await conn.execute(
            "SELECT speech_act, speech_act_confidence, assertion_provenance, "
            "durability, expires_at FROM memory_metadata WHERE memory_id='mw1-b'"
        )
        row = await cur.fetchone()
        assert row == (None, None, None, None, None)
    finally:
        await conn.close()
