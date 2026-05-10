"""Tests for reference migration from knowledge_base to episodic_memory."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables

_migration = importlib.import_module("genesis.db.migrations.0013_migrate_refs_to_episodic")
up = _migration.up


@pytest.fixture
async def db_with_refs():
    """In-memory DB with schema + simulated reference entries in knowledge_base."""
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()

        # Insert a reference entry into knowledge_units
        await db.execute(
            "INSERT INTO knowledge_units "
            "(id, project_type, domain, source_doc, concept, body, tags, "
            " ingested_at, qdrant_id, confidence, embedding_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "unit-ref-1", "reference", "reference.credentials",
                "reference_store:manual", "Acme API key",
                "[reference.credentials] Acme API key\nValue: sk-123",
                '["reference", "credentials"]',
                "2026-05-01T00:00:00", "qdrant-ref-1", 0.85, "test-embed",
            ),
        )

        # Insert matching memory_metadata row (as store.store() would create)
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, collection, "
            "confidence, embedding_status, memory_class) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("qdrant-ref-1", "2026-05-01T00:00:00", "knowledge_base", 0.85,
             "embedded", "fact"),
        )

        # Insert matching memory_fts row
        await db.execute(
            "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
            "VALUES (?, ?, ?, ?, ?)",
            ("qdrant-ref-1", "Acme API key sk-123", "memory", "reference credentials",
             "knowledge_base"),
        )

        # Insert a non-reference knowledge entry (should NOT be migrated)
        await db.execute(
            "INSERT INTO knowledge_units "
            "(id, project_type, domain, source_doc, concept, body, tags, "
            " ingested_at, qdrant_id, confidence, embedding_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "unit-kb-1", "cloud-eng", "aws-vpc",
                "manual", "VPC subnets",
                "VPC subnets are CIDR subdivisions",
                '["aws", "vpc"]',
                "2026-05-01T00:00:00", "qdrant-kb-1", 0.85, "test-embed",
            ),
        )
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, collection, "
            "confidence, embedding_status, memory_class) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("qdrant-kb-1", "2026-05-01T00:00:00", "knowledge_base", 0.85,
             "embedded", "fact"),
        )
        await db.execute(
            "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
            "VALUES (?, ?, ?, ?, ?)",
            ("qdrant-kb-1", "VPC subnets", "memory", "aws vpc", "knowledge_base"),
        )

        await db.commit()
        yield db


async def test_migration_moves_reference_metadata(db_with_refs):
    """Migration updates collection for reference entries in memory_metadata."""
    await up(db_with_refs)

    cursor = await db_with_refs.execute(
        "SELECT collection FROM memory_metadata WHERE memory_id = 'qdrant-ref-1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "episodic_memory"


async def test_migration_moves_reference_fts(db_with_refs):
    """Migration updates collection for reference entries in memory_fts."""
    await up(db_with_refs)

    cursor = await db_with_refs.execute(
        "SELECT collection FROM memory_fts WHERE memory_id = 'qdrant-ref-1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "episodic_memory"


async def test_migration_leaves_non_reference_unchanged(db_with_refs):
    """Non-reference knowledge entries stay in knowledge_base."""
    await up(db_with_refs)

    cursor = await db_with_refs.execute(
        "SELECT collection FROM memory_metadata WHERE memory_id = 'qdrant-kb-1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "knowledge_base"

    cursor = await db_with_refs.execute(
        "SELECT collection FROM memory_fts WHERE memory_id = 'qdrant-kb-1'"
    )
    row = await cursor.fetchone()
    assert row[0] == "knowledge_base"


async def test_migration_idempotent(db_with_refs):
    """Running the migration twice produces the same result."""
    await up(db_with_refs)
    await up(db_with_refs)  # Second run should be a no-op

    cursor = await db_with_refs.execute(
        "SELECT collection FROM memory_metadata WHERE memory_id = 'qdrant-ref-1'"
    )
    assert (await cursor.fetchone())[0] == "episodic_memory"

    cursor = await db_with_refs.execute(
        "SELECT collection FROM memory_metadata WHERE memory_id = 'qdrant-kb-1'"
    )
    assert (await cursor.fetchone())[0] == "knowledge_base"


async def test_migration_fresh_install_noop():
    """On a fresh install with no knowledge_units table, migration is a no-op."""
    async with aiosqlite.connect(":memory:") as db:
        # Minimal schema without knowledge_units
        await db.execute(
            "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, "
            "created_at TEXT, collection TEXT, confidence REAL, "
            "embedding_status TEXT, memory_class TEXT)"
        )
        await db.commit()
        # Should not raise
        await up(db)
