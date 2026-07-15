"""search_ranked must filter out rows past their invalid_at."""

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
        # (memory_id, invalid_at, content) — all contain "row" for FTS match
        ("alive", None, "this row never expires"),
        ("future", "2099-01-01T00:00:00+00:00", "this row is valid until 2099"),
        ("expired", "2020-01-01T00:00:00+00:00", "this row expired in 2020"),
    ]
    for mid, inv, content in rows:
        await conn.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, invalid_at) VALUES (?, ?, ?)",
            (mid, "2020-01-01T00:00:00+00:00", inv),
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
async def test_default_filters_expired(tmp_path) -> None:
    """Default as_of (now) drops 'expired', keeps 'alive' + 'future'."""
    conn = await _build_db(str(tmp_path / "t.db"))
    try:
        rows = await memory_crud.search_ranked(conn, query="row")
        ids = {r["memory_id"] for r in rows}
        assert ids == {"alive", "future"}
        assert "expired" not in ids
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_as_of_in_past_keeps_expired(tmp_path) -> None:
    """An explicit historical `as_of` returns rows valid at that point."""
    conn = await _build_db(str(tmp_path / "t.db"))
    try:
        rows = await memory_crud.search_ranked(
            conn,
            query="row",
            as_of="2019-01-01T00:00:00+00:00",
        )
        ids = {r["memory_id"] for r in rows}
        # At 2019, all three were valid (expired was valid until 2020-01-01)
        assert ids == {"alive", "future", "expired"}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_invalid_at_combines_with_subsystem_filter(tmp_path) -> None:
    """invalid_at + exclude_subsystems both apply on the same JOIN."""
    conn = await _build_db(str(tmp_path / "t.db"))
    try:
        # Tag 'future' as a subsystem write so it gets excluded too
        await conn.execute(
            "UPDATE memory_metadata SET source_subsystem='reflection' WHERE memory_id='future'"
        )
        await conn.commit()

        rows = await memory_crud.search_ranked(
            conn,
            query="row",
            exclude_subsystems=["ego", "triage", "reflection"],
        )
        ids = {r["memory_id"] for r in rows}
        # alive: NULL invalid_at + NULL subsystem → kept
        # future: NULL invalid_at fine, but reflection → excluded
        # expired: invalid_at past → excluded
        assert ids == {"alive"}
    finally:
        await conn.close()


async def test_origin_class_by_ids(tmp_path):
    """WS-3 B4: batch memory_id → stored origin_class lookup (used by
    memory_core_facts to recover backfilled values for stale Qdrant payloads)."""
    conn = await _build_db(str(tmp_path / "o.db"))
    try:
        await conn.execute(
            "UPDATE memory_metadata SET origin_class='external_untrusted' WHERE memory_id='alive'"
        )
        await conn.commit()

        got = await memory_crud.origin_class_by_ids(conn, ["alive", "expired", "nope"])
        assert got["alive"] == "external_untrusted"
        assert got["expired"] is None  # row exists, origin NULL
        assert "nope" not in got  # missing id omitted
        assert await memory_crud.origin_class_by_ids(conn, []) == {}
    finally:
        await conn.close()


async def test_origin_class_by_ids_chunks_past_bind_cap(tmp_path):
    """Codex #1048 P2: a large id list (memory_expand / core_facts scroll) must
    NOT breach SQLite's 999 bind-variable cap — the helper chunks so origin
    recovery stays reliable at any scale (a raised 'too many SQL variables'
    would fail the callers open to origin_class=None, slipping the gate)."""
    conn = await _build_db(str(tmp_path / "chunk.db"))
    try:
        n = 2500  # well past the 999 cap and the 900 chunk size
        for i in range(n):
            oc = "external_untrusted" if i % 2 == 0 else "first_party"
            await conn.execute(
                "INSERT INTO memory_metadata (memory_id, created_at, origin_class) "
                "VALUES (?, ?, ?)",
                (f"m{i}", "2026-01-01T00:00:00+00:00", oc),
            )
        await conn.commit()

        ids = [f"m{i}" for i in range(n)] + ["absent-1", "absent-2"]
        got = await memory_crud.origin_class_by_ids(conn, ids)
        assert len(got) == n  # every present id resolved across chunks
        assert got["m0"] == "external_untrusted"
        assert got["m1"] == "first_party"
        assert "absent-1" not in got
    finally:
        await conn.close()
