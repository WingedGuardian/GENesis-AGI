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
            origin_class TEXT,
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


@pytest.mark.asyncio
async def test_rows_carry_origin_class(tmp_path) -> None:
    """WS-3 B4: search_ranked returns memory_metadata.origin_class (the FTS5
    path's only route to the stored provenance; NULL for pre-0054 rows)."""
    conn = await _build_db(str(tmp_path / "fts.db"))
    try:
        await conn.execute(
            "UPDATE memory_metadata SET origin_class = 'external_untrusted' "
            "WHERE memory_id = 'user-1'"
        )
        await conn.commit()
        rows = await memory_crud.search_ranked(
            conn, query="decision recovery", include_only_subsystems=None,
        )
        by_id = {r["memory_id"]: r for r in rows}
        assert by_id["user-1"]["origin_class"] == "external_untrusted"
        assert by_id["ego-1"]["origin_class"] is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_rows_carry_wing_and_room(tmp_path) -> None:
    """search_ranked projects the authoritative memory_metadata.wing/room so
    the FTS path can scope-filter fts5_only candidates (follow-up 0a3741c4)."""
    conn = await _build_db(str(tmp_path / "fts.db"))
    try:
        await conn.execute(
            "UPDATE memory_metadata SET wing = 'routing', room = 'fallback' "
            "WHERE memory_id = 'user-1'"
        )
        await conn.commit()
        rows = await memory_crud.search_ranked(
            conn, query="decision recovery", include_only_subsystems=None,
        )
        by_id = {r["memory_id"]: r for r in rows}
        assert by_id["user-1"]["wing"] == "routing"
        assert by_id["user-1"]["room"] == "fallback"
        # FTS tag string is projected too (empty for these seed rows) — the
        # scope filter reads it to honor explicit life_domain overrides.
        assert by_id["user-1"]["tags"] == ""
        # Unclassified rows project NULL wing/room, not a missing key.
        assert by_id["ego-1"]["wing"] is None
        assert by_id["ego-1"]["room"] is None
    finally:
        await conn.close()
