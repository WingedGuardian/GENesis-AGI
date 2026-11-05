"""reference_secrets: the numbered migration and the base schema path agree.

The migration must stand alone (the standalone runner applies it without
create_all_tables), be idempotent, and produce exactly the table and triggers a
fresh install gets.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.db.schema._tables import TABLES

MIG = importlib.import_module("genesis.db.migrations.20261105043443_reference_secrets")

_OBJECTS = (
    "reference_secrets",
    "reference_secrets_retire",
    "knowledge_units_credential_body_insert",
    "knowledge_units_credential_body_update",
)


def _normalise(sql: str) -> str:
    return " ".join(sql.split())


async def _schema(db) -> dict[str, str]:
    rows = await db.execute_fetchall(
        "SELECT name, sql FROM sqlite_master WHERE name IN (?, ?, ?, ?)",
        _OBJECTS,
    )
    return {name: _normalise(sql) for name, sql in rows}


async def test_the_migration_alone_matches_a_fresh_install():
    async with aiosqlite.connect(":memory:") as fresh:
        await create_all_tables(fresh)
        fresh_schema = await _schema(fresh)
    async with aiosqlite.connect(":memory:") as legacy:
        # A database from before the change: knowledge_units, nothing else.
        await legacy.execute(TABLES["knowledge_units"])
        await MIG.up(legacy)
        await MIG.up(legacy)  # idempotent
        migrated = await _schema(legacy)
    assert set(fresh_schema) == set(_OBJECTS)
    assert migrated == fresh_schema


def test_the_migration_carries_the_placeholder_the_code_writes():
    assert MIG._PLACEHOLDER == kc.SECRET_PLACEHOLDER
    assert len("\n\nValue: ") == MIG._MARKER_LEN


async def test_the_migration_without_knowledge_units_creates_only_the_table():
    async with aiosqlite.connect(":memory:") as db:
        await MIG.up(db)
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%'"
        )
    assert {r[0] for r in rows} == {"reference_secrets"}


async def test_a_migrated_database_refuses_an_inline_credential_body():
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(TABLES["knowledge_units"])
        await MIG.up(db)
        with pytest.raises(aiosqlite.IntegrityError, match="carries its value"):
            await db.execute(
                "INSERT INTO knowledge_units (id, project_type, domain, source_doc, "
                "concept, body, ingested_at) VALUES ('x', 'reference', "
                "'reference.credentials', 'x', 'c', ?, 'now')",
                ("[reference.credentials] c\n\nd\n\nValue: inline-fake-9",),
            )
