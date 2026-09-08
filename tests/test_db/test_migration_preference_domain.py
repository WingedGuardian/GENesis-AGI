"""preference_domain — migration + persistence (MW-4 satellite).

Mirrors the 0081 judgment-column tests: the column exists on BOTH schema build
paths (numbered migration for legacy DBs, canonical CREATE for fresh ones), the
migration is idempotent, and ``create_metadata`` persists the value.
"""

from __future__ import annotations

import importlib

import aiosqlite

from genesis.db.crud import memory
from genesis.db.schema import TABLES

_mig = importlib.import_module("genesis.db.migrations.20260906042425_preference_domain")

_COL = "preference_domain"

# memory_metadata as it stands BEFORE this column (i.e. post-0081).
_PRE_DDL = """
    CREATE TABLE memory_metadata (
        memory_id             TEXT PRIMARY KEY,
        created_at            TEXT NOT NULL,
        collection            TEXT NOT NULL DEFAULT 'episodic_memory',
        confidence            REAL,
        embedding_status      TEXT NOT NULL DEFAULT 'embedded',
        memory_class          TEXT DEFAULT 'fact',
        valid_at              TEXT,
        invalid_at            TEXT,
        origin_class          TEXT,
        speech_act            TEXT,
        speech_act_confidence REAL,
        assertion_provenance  TEXT,
        durability            TEXT,
        expires_at            TEXT
    )
"""


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_migration_adds_the_column_to_an_existing_db():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_PRE_DDL)
        await conn.commit()
        assert _COL not in await _columns(conn, "memory_metadata")

        await _mig.up(conn)
        await conn.commit()

        assert _COL in await _columns(conn, "memory_metadata")
    finally:
        await conn.close()


async def test_migration_is_idempotent():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_PRE_DDL)
        await conn.commit()
        await _mig.up(conn)
        await _mig.up(conn)  # second run must not raise
        await conn.commit()
        assert _COL in await _columns(conn, "memory_metadata")
    finally:
        await conn.close()


async def test_migration_skips_a_db_without_the_table():
    """Fresh install: create_all_tables builds the canonical shape; the
    migration must no-op rather than raise on the missing table."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await _mig.up(conn)  # no memory_metadata at all
        await conn.commit()
    finally:
        await conn.close()


async def test_fresh_db_create_carries_the_column():
    """schema_both_build_paths: the canonical CREATE must carry it too, or a
    fresh install's first create_metadata INSERT hits 'no such column'."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_metadata"])
        await conn.commit()
        assert _COL in await _columns(conn, "memory_metadata")
    finally:
        await conn.close()


async def test_create_metadata_persists_the_domain():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_metadata"])
        await conn.commit()
        await memory.create_metadata(
            conn,
            memory_id="m-pref",
            created_at="2026-09-06T00:00:00+00:00",
            speech_act="preference",
            preference_domain="work",
        )
        cur = await conn.execute(
            "SELECT preference_domain FROM memory_metadata WHERE memory_id = ?", ("m-pref",)
        )
        assert (await cur.fetchone())[0] == "work"
    finally:
        await conn.close()


async def test_create_metadata_defaults_null():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_metadata"])
        await conn.commit()
        await memory.create_metadata(
            conn, memory_id="m-plain", created_at="2026-09-06T00:00:00+00:00"
        )
        cur = await conn.execute(
            "SELECT preference_domain FROM memory_metadata WHERE memory_id = ?", ("m-plain",)
        )
        assert (await cur.fetchone())[0] is None
    finally:
        await conn.close()
