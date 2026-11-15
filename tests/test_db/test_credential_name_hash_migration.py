"""knowledge_units.name_hash / name_origin: the migration and the base path agree.

The migration stands alone (the runner applies it without create_all_tables),
backfills every existing credential from its key, is idempotent, and leaves
the partial unique index a fresh install gets. Fake names only.
"""

from __future__ import annotations

import importlib

import aiosqlite

from genesis.db.schema import create_all_tables
from genesis.db.schema._tables import TABLES
from genesis.memory.reference_ops import name_hash

MIG = importlib.import_module("genesis.db.migrations.20261115060803_credential_name_hash")
_LEGACY_UNITS = (
    TABLES["knowledge_units"]
    .replace("name_hash        TEXT,\n", "")
    .replace("name_origin      TEXT,\n", "")
)


async def _columns(db) -> list[str]:
    return [r[1] for r in await db.execute_fetchall("PRAGMA table_info(knowledge_units)")]


async def _index(db) -> str | None:
    rows = await db.execute_fetchall(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_knowledge_units_name_hash'"
    )
    return " ".join(rows[0][0].split()) if rows else None


async def _legacy_db(db) -> None:
    await db.execute(_LEGACY_UNITS)
    for i, (concept, body) in enumerate(
        (
            ("lab wifi", "b"),
            ("vpn admin #a1b2c3", "b"),  # a key that merely ends like a tag: its own name
            ("cred-0123456789ab", "[reference.credentials] db admin\n\nrest"),
            ("Card 4111 1111 1111 1111 PIN", "b"),
            ("LAB WIFI", "b"),
            ("Lab  WiFi", "b"),
        )
    ):
        await db.execute(
            "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
            "ingested_at) VALUES (?, 'reference', 'reference.credentials', 'x', ?, ?, 'now')",
            (f"u{i}", concept, body),
        )
    # Not a credential: left alone.
    await db.execute(
        "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, ingested_at) "
        "VALUES ('n', 'reference', 'reference.url', 'x', 'lab wifi', 'b', 'now')"
    )


async def test_both_build_paths_carry_the_columns_and_the_index():
    async with aiosqlite.connect(":memory:") as fresh:
        await create_all_tables(fresh)
        fresh_index = await _index(fresh)
        assert {"name_hash", "name_origin"} <= set(await _columns(fresh))
        await MIG.up(fresh)  # after create_all_tables: a no-op, no collision
        assert (await _columns(fresh)).count("name_hash") == 1
    async with aiosqlite.connect(":memory:") as legacy:
        await legacy.execute(_LEGACY_UNITS)
        assert "name_hash" not in await _columns(legacy)
        await MIG.up(legacy)
        assert {"name_hash", "name_origin"} <= set(await _columns(legacy))
        assert await _index(legacy) == fresh_index
        assert fresh_index == " ".join(MIG.INDEX.replace("IF NOT EXISTS ", "").split())


async def test_the_backfill_records_each_credentials_identity_once():
    async with aiosqlite.connect(":memory:") as db:
        await _legacy_db(db)
        await MIG.up(db)
        first = await db.execute_fetchall(
            "SELECT id, name_hash, name_origin FROM knowledge_units ORDER BY id"
        )
        await MIG.up(db)  # idempotent
        again = await db.execute_fetchall(
            "SELECT id, name_hash, name_origin FROM knowledge_units ORDER BY id"
        )
        assert again == first
    wifi = name_hash("lab wifi")
    assert {r[0]: (r[1], r[2]) for r in first} == {
        "u0": (wifi, "trusted"),
        "u1": (name_hash("vpn admin #a1b2c3"), "trusted"),
        "u2": (name_hash("db admin"), "trusted"),
        "u3": (name_hash("Card 4111 1111 1111 1111 PIN"), "carded"),
        # The same name in another case, created later: kept, out of the index.
        "u4": (wifi + "#later", "trusted"),
        "u5": (wifi + "#later", "trusted"),
        "n": (None, None),
    }


async def test_the_migration_without_knowledge_units_is_a_no_op():
    async with aiosqlite.connect(":memory:") as db:
        await MIG.up(db)
        assert await db.execute_fetchall("SELECT name FROM sqlite_master") == []
