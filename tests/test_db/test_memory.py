"""Tests for memory CRUD (FTS5-based)."""

import sqlite3

import pytest

from genesis.db.crud import memory

# FTS5 may not be available in in-memory SQLite.
_fts5_available = True
try:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE _fts5_test USING fts5(x)")
    conn.close()
except Exception:
    _fts5_available = False

pytestmark = pytest.mark.skipif(not _fts5_available, reason="FTS5 not available")


async def test_create_and_search(db):
    await memory.create(db, memory_id="m1", content="hello world test")
    results = await memory.search(db, query="hello")
    assert len(results) >= 1
    assert results[0]["memory_id"] == "m1"


async def test_search_with_filters(db):
    await memory.create(db, memory_id="m2", content="alpha beta", source_type="note", collection="col1")
    await memory.create(db, memory_id="m3", content="alpha gamma", source_type="log", collection="col2")
    results = await memory.search(db, query="alpha", source_type="note")
    assert all(r["source_type"] == "note" for r in results)
    results = await memory.search(db, query="alpha", collection="col2")
    assert all(r["collection"] == "col2" for r in results)


async def test_search_empty_results(db):
    results = await memory.search(db, query="nonexistentxyz")
    assert results == []


async def test_delete_existing(db):
    await memory.create(db, memory_id="m4", content="delete me")
    assert await memory.delete(db, memory_id="m4") is True


async def test_delete_nonexistent(db):
    assert await memory.delete(db, memory_id="nope") is False


async def test_search_limit(db):
    for i in range(5):
        await memory.create(db, memory_id=f"lim{i}", content=f"limitword item {i}")
    results = await memory.search(db, query="limitword", limit=3)
    assert len(results) <= 3


async def test_search_ranked(db):
    await memory.create(db, memory_id="r1", content="ranked search testing")
    results = await memory.search_ranked(db, query="ranked")
    assert len(results) >= 1
    assert "rank" in results[0]
    assert results[0]["memory_id"] == "r1"


async def test_search_ranked_with_collection(db):
    await memory.create(
        db, memory_id="r2", content="ranked col filter", collection="special",
    )
    results = await memory.search_ranked(db, query="ranked", collection="special")
    assert all(r["collection"] == "special" for r in results)


async def test_get_taxonomy(db):
    await memory.create_metadata(
        db, memory_id="tx1", created_at="2020-01-01T00:00:00+00:00",
        wing="infrastructure", room="watchdog", origin_class="first_party",
    )
    assert await memory.get_taxonomy(db, "tx1") == {
        "wing": "infrastructure", "room": "watchdog",
        "origin_class": "first_party",
    }
    # room/origin_class optional — a wing-only row still resolves
    await memory.create_metadata(
        db, memory_id="tx2", created_at="2020-01-01T00:00:00+00:00", wing="memory",
    )
    assert await memory.get_taxonomy(db, "tx2") == {
        "wing": "memory", "room": None, "origin_class": None,
    }
    assert await memory.get_taxonomy(db, "missing") is None


async def test_batch_created_at(db):
    await memory.create_metadata(
        db, memory_id="c1", created_at="2020-01-01T00:00:00+00:00",
    )
    await memory.create_metadata(
        db, memory_id="c2", created_at="2021-02-02T00:00:00+00:00",
    )
    got = await memory.batch_created_at(db, ["c1", "c2", "missing"])
    assert got == {
        "c1": "2020-01-01T00:00:00+00:00",
        "c2": "2021-02-02T00:00:00+00:00",
    }
    # missing ids are simply omitted; empty input short-circuits
    assert "missing" not in got
    assert await memory.batch_created_at(db, []) == {}
