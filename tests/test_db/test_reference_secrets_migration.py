"""reference_secrets: the numbered migration and the base schema path agree.

The migration must stand alone (the standalone runner applies it without
create_all_tables), be idempotent, and produce exactly the table, the
knowledge_units.secret_ref column, its index and the triggers a fresh install
gets.
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
    "idx_knowledge_units_secret_ref",
    "knowledge_units_secret_ref_insert",
    "knowledge_units_secret_ref_update",
    "knowledge_units_secret_ref_repoint",
    "knowledge_units_secret_ref_delete",
    "knowledge_units_credential_body_insert",
    "knowledge_units_credential_body_update",
)

#: knowledge_units as a database from before this change has it: no secret_ref.
_LEGACY_UNITS = TABLES["knowledge_units"].replace("secret_ref       TEXT,\n", "")


def _normalise(sql: str) -> str:
    return " ".join(sql.split())


async def _schema(db) -> dict[str, str]:
    marks = ", ".join("?" for _ in _OBJECTS)
    rows = await db.execute_fetchall(
        f"SELECT name, sql FROM sqlite_master WHERE name IN ({marks})",  # noqa: S608 - placeholders only
        _OBJECTS,
    )
    return {name: _normalise(sql) for name, sql in rows}


async def _columns(db) -> list[str]:
    return [r[1] for r in await db.execute_fetchall("PRAGMA table_info(knowledge_units)")]


def test_the_legacy_fixture_really_lacks_the_column():
    assert "secret_ref" in TABLES["knowledge_units"]
    assert "secret_ref" not in _LEGACY_UNITS


async def test_the_migration_alone_matches_a_fresh_install():
    async with aiosqlite.connect(":memory:") as fresh:
        await create_all_tables(fresh)
        fresh_schema = await _schema(fresh)
        fresh_columns = await _columns(fresh)
    async with aiosqlite.connect(":memory:") as legacy:
        # A database from before the change: knowledge_units, nothing else.
        await legacy.execute(_LEGACY_UNITS)
        await MIG.up(legacy)
        await MIG.up(legacy)  # idempotent
        migrated = await _schema(legacy)
        migrated_columns = await _columns(legacy)
    assert set(fresh_schema) == set(_OBJECTS)
    assert migrated == fresh_schema
    assert migrated_columns == fresh_columns


async def test_the_migration_after_a_fresh_install_is_a_no_op():
    """The runner applies it at boot after create_all_tables has added the
    column: the guarded ALTER must not collide."""
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        before = await _schema(db)
        await MIG.up(db)
        assert await _schema(db) == before
        assert (await _columns(db)).count("secret_ref") == 1


def test_the_migration_carries_the_ddl_the_code_writes():
    assert MIG._PLACEHOLDER == kc.SECRET_PLACEHOLDER
    assert len("\n\nValue: ") == MIG._MARKER_LEN
    assert tuple(map(_normalise, MIG.SECRET_REF_TRIGGERS)) == tuple(
        map(_normalise, kc.secret_ref_trigger_ddl())
    )
    assert tuple(map(_normalise, MIG.BODY_TRIGGERS)) == tuple(
        map(_normalise, kc.credential_body_trigger_ddl())
    )


async def test_the_migration_without_knowledge_units_creates_only_the_table():
    async with aiosqlite.connect(":memory:") as db:
        await MIG.up(db)
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%'"
        )
    assert {r[0] for r in rows} == {"reference_secrets"}


async def test_a_migrated_database_refuses_an_inline_credential_body():
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(_LEGACY_UNITS)
        await MIG.up(db)
        with pytest.raises(aiosqlite.IntegrityError, match="carries its value"):
            await db.execute(
                "INSERT INTO knowledge_units (id, project_type, domain, source_doc, "
                "concept, body, ingested_at) VALUES ('x', 'reference', "
                "'reference.credentials', 'x', 'c', ?, 'now')",
                ("[reference.credentials] c\n\nd\n\nValue: inline-fake-9",),
            )


async def test_a_migrated_legacy_row_keeps_updating_its_counters():
    """A credential row from before the change (value inline, no secret_ref)
    still takes an update of a column the guards do not watch."""
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(_LEGACY_UNITS)
        await db.execute(
            "INSERT INTO knowledge_units (id, project_type, domain, source_doc, "
            "concept, body, ingested_at) VALUES ('old', 'reference', "
            "'reference.credentials', 'x', 'c', ?, 'now')",
            ("[reference.credentials] c\n\nd\n\nValue: inline-fake-9",),
        )
        await MIG.up(db)
        await db.execute(
            "UPDATE knowledge_units SET retrieved_count = retrieved_count + 1 WHERE id = 'old'"
        )
        rows = await db.execute_fetchall("SELECT retrieved_count, secret_ref FROM knowledge_units")
    assert [tuple(r) for r in rows] == [(1, None)]


async def _secret_columns(db) -> list[str]:
    return [r[1] for r in await db.execute_fetchall("PRAGMA table_info(reference_secrets)")]


async def test_both_build_paths_carry_the_detail_column():
    """The held-out description (``detail``) sits in the same immutable row as
    the value, on a fresh install and after the migration alone."""
    async with aiosqlite.connect(":memory:") as fresh:
        await create_all_tables(fresh)
        assert await _secret_columns(fresh) == ["id", "value", "detail", "created_at"]
    async with aiosqlite.connect(":memory:") as migrated:
        await MIG.up(migrated)
        assert await _secret_columns(migrated) == ["id", "value", "detail", "created_at"]


async def test_a_secrets_table_from_before_the_detail_column_gains_it():
    """A reference_secrets table this unmerged migration created earlier, with no
    detail column: the migration adds it, idempotently, keeping its rows."""
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE reference_secrets (id TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        await db.execute("INSERT INTO reference_secrets VALUES ('s1', 'fake-1', 'now')")
        await MIG.up(db)
        await MIG.up(db)
        assert "detail" in await _secret_columns(db)
        rows = await db.execute_fetchall("SELECT id, value, detail FROM reference_secrets")
        assert [tuple(r) for r in rows] == [("s1", "fake-1", None)]
