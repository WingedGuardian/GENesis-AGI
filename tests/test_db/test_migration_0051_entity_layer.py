"""Migration 0051 — entity layer tables (entities/mentions/links)."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M51 = importlib.import_module("genesis.db.migrations.0051_entity_layer")

_TABLES = ("entities", "entity_mentions", "entity_links")


async def _tables(db: aiosqlite.Connection) -> set[str]:
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {r[0] for r in rows}


async def _indexes(db: aiosqlite.Connection) -> set[str]:
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_up_creates_tables_and_indexes():
    db = await aiosqlite.connect(":memory:")
    try:
        await M51.up(db)
        tables = await _tables(db)
        assert all(t in tables for t in _TABLES)
        indexes = await _indexes(db)
        assert "idx_entities_norm" in indexes
        assert "idx_entity_mentions_entity" in indexes
        assert "idx_entity_links_target" in indexes
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_idempotent():
    db = await aiosqlite.connect(":memory:")
    try:
        await M51.up(db)
        await M51.up(db)  # must not raise
        tables = await _tables(db)
        assert all(t in tables for t in _TABLES)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_down_drops():
    db = await aiosqlite.connect(":memory:")
    try:
        await M51.up(db)
        await M51.down(db)
        assert not (await _tables(db)) & set(_TABLES)
    finally:
        await db.close()
