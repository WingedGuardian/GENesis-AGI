"""search_ranked subsystem-filter JOIN behavior."""

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
        ("user-1",   None,         "decision recovery context"),
        ("ego-1",    "ego",        "decision recovery proposal"),
        ("triage-1", "triage",     "decision recovery signal"),
        ("refl-1",   "reflection", "decision recovery observation"),
    ]
    for mid, sub, content in rows:
        await conn.execute(
            "INSERT INTO memory_metadata "
            "(memory_id, created_at, source_subsystem) VALUES (?, ?, ?)",
            (mid, "2026-05-10T00:00:00Z", sub),
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
async def test_default_no_filter_returns_all(tmp_path) -> None:
    conn = await _build_db(str(tmp_path / "fts.db"))
    try:
        rows = await memory_crud.search_ranked(conn, query="decision")
        ids = {r["memory_id"] for r in rows}
        assert ids == {"user-1", "ego-1", "triage-1", "refl-1"}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_exclude_subsystems(tmp_path) -> None:
    conn = await _build_db(str(tmp_path / "fts.db"))
    try:
        rows = await memory_crud.search_ranked(
            conn, query="decision",
            exclude_subsystems=["ego", "triage", "reflection"],
        )
        ids = {r["memory_id"] for r in rows}
        assert ids == {"user-1"}, (
            "NULL source_subsystem (user-sourced) must pass the exclude filter"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_exclude_some(tmp_path) -> None:
    """Excluding only 'ego' keeps user + triage + reflection."""
    conn = await _build_db(str(tmp_path / "fts.db"))
    try:
        rows = await memory_crud.search_ranked(
            conn, query="decision",
            exclude_subsystems=["ego"],
        )
        ids = {r["memory_id"] for r in rows}
        assert ids == {"user-1", "triage-1", "refl-1"}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_include_only(tmp_path) -> None:
    conn = await _build_db(str(tmp_path / "fts.db"))
    try:
        rows = await memory_crud.search_ranked(
            conn, query="decision",
            include_only_subsystems=["ego"],
        )
        ids = {r["memory_id"] for r in rows}
        assert ids == {"ego-1"}, (
            "include_only must drop NULL (user) rows entirely"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_include_only_multiple(tmp_path) -> None:
    conn = await _build_db(str(tmp_path / "fts.db"))
    try:
        rows = await memory_crud.search_ranked(
            conn, query="decision",
            include_only_subsystems=["ego", "triage"],
        )
        ids = {r["memory_id"] for r in rows}
        assert ids == {"ego-1", "triage-1"}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_collection_filter_combines_with_subsystem(tmp_path) -> None:
    """collection filter and subsystem filter must both apply."""
    conn = await _build_db(str(tmp_path / "fts.db"))
    try:
        # All seed rows are episodic_memory, so this still excludes ego/triage/refl
        rows = await memory_crud.search_ranked(
            conn, query="decision",
            collection="episodic_memory",
            exclude_subsystems=["ego", "triage", "reflection"],
        )
        ids = {r["memory_id"] for r in rows}
        assert ids == {"user-1"}
    finally:
        await conn.close()
