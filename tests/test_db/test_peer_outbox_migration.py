"""Frozen upgrade DDL and access paths for indefinitely retained peer messages."""

import importlib

import pytest


async def test_outbox_migration_does_not_read_future_canonical_ddl(db, monkeypatch):
    from genesis.db.schema._tables import TABLES

    await db.execute("DROP TABLE peer_outbox")
    monkeypatch.setitem(TABLES, "peer_outbox", "CREATE TABLE future_outbox_probe(id TEXT)")
    migration = importlib.import_module("genesis.db.migrations.20261015005200_peer_outbox")
    await migration.up(db)
    tables = {
        row[0]
        for row in await (
            await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    assert "peer_outbox" in tables
    assert "future_outbox_probe" not in tables


@pytest.mark.parametrize("build", ["fresh", "migration"])
async def test_retained_outbox_collection_and_context_use_ordered_indexes(db, build):
    if build == "migration":
        await db.execute("DROP TABLE peer_outbox")
        migration = importlib.import_module("genesis.db.migrations.20261015005200_peer_outbox")
        await migration.up(db)
        await migration.up(db)  # Additive upgrades remain idempotent.
    paths = [
        (
            "SELECT * FROM peer_outbox WHERE peer_id=? AND epoch=? AND acknowledged_at IS NULL ORDER BY created_at,message_id LIMIT ?",
            ("alpha", "epoch", 21),
            "idx_peer_outbox_collect",
        ),
        (
            "SELECT * FROM peer_outbox WHERE peer_id=? AND epoch=? AND acknowledged_at IS NULL AND (created_at,message_id)>(?,?) ORDER BY created_at,message_id LIMIT ?",
            ("alpha", "epoch", "time", "id", 21),
            "idx_peer_outbox_collect",
        ),
        (
            "SELECT * FROM peer_outbox WHERE peer_id=? AND epoch=? AND context_id=? ORDER BY created_at DESC,message_id DESC LIMIT 9",
            ("alpha", "epoch", "context"),
            "idx_peer_outbox_context",
        ),
        (
            "SELECT 1 FROM peer_outbox WHERE peer_id=? AND epoch=? AND context_id=? LIMIT 1",
            ("alpha", "epoch", "context"),
            "idx_peer_outbox_context",
        ),
    ]
    for sql, params, expected in paths:
        plan = " ".join(
            row[3]
            for row in await (await db.execute("EXPLAIN QUERY PLAN " + sql, params)).fetchall()
        )
        assert expected in plan, plan
        assert "USE TEMP B-TREE" not in plan, plan
