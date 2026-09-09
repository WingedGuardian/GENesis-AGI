"""Tests for memory CRUD (FTS5-based)."""

import logging
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


async def test_search_is_and_only_no_fallback(db):
    # memory.search is DELIBERATELY AND-only (no OR-fallback): its only caller is
    # the entity-name resolver, which must not be widened to single-term matches.
    # A partial-term query returns NOTHING (contrast search_ranked below).
    await memory.create(db, memory_id="p1", content="alpha beta gamma delta")
    assert await memory.search(db, query="alpha nonexistentword") == []
    # A fully-present query still hits (basic AND path intact).
    assert [r["memory_id"] for r in await memory.search(db, query="alpha beta")] == ["p1"]


async def test_search_ranked_degrades_on_unparseable_boolean_query(db, caplog):
    """An FTS5-invalid boolean expression must DEGRADE, never raise.

    Every known producer now sanitises its terms before joining, so this path is
    reached only by a future producer emitting something malformed. It used to
    escape as sqlite3.OperationalError and surface as HTTP 500 from the recall
    endpoint. The retry drops to bare terms: expansion is an optimisation, and
    losing its precision beats losing the query.
    """
    await memory.create(db, memory_id="d1", content="alpha beta gamma")
    # Parenthesis-BALANCED but structurally invalid — the exact shape a
    # punctuation-only term used to leave behind. Balance is why the old
    # paren-counting check waved it through.
    bad = "(alpha) OR (beta OR )"
    with caplog.at_level(logging.WARNING, logger="genesis.db.crud.memory"):
        results = await memory.search_ranked(db, query=bad, boolean=True)
    assert [r["memory_id"] for r in results] == ["d1"]
    assert "FTS5 rejected a composed boolean query" in caplog.text


async def test_search_ranked_real_db_error_still_raises(db):
    """The backstop is narrowed to fts5 SYNTAX errors — it must not swallow a
    genuine database failure into a silent empty result. Querying a table that
    does not exist is an OperationalError that is NOT an fts5 syntax error.
    """
    with pytest.raises(sqlite3.OperationalError):
        await db.execute_fetchall("SELECT 1 FROM no_such_table_xyz WHERE x MATCH ?", ["a"])


async def test_search_ranked_or_fallback_on_partial_terms(db):
    await memory.create(db, memory_id="p3", content="alpha beta gamma delta")
    results = await memory.search_ranked(db, query="alpha nonexistentword")
    assert [r["memory_id"] for r in results] == ["p3"]


async def test_search_ranked_boolean_true_skips_fallback(db):
    # Default (raw) query surfaces via OR-fallback; the SAME query with
    # boolean=True is treated as a structured expression and must NOT fall back,
    # so it stays empty — proving the guard.
    await memory.create(db, memory_id="p4", content="alpha beta gamma")
    assert [r["memory_id"] for r in await memory.search_ranked(db, query="alpha zzz")] == ["p4"]
    assert await memory.search_ranked(db, query="alpha zzz", boolean=True) == []


async def test_get_taxonomy(db):
    await memory.create_metadata(
        db, memory_id="tx1", created_at="2020-01-01T00:00:00+00:00",
        wing="infrastructure", room="watchdog", origin_class="first_party",
    )
    assert await memory.get_taxonomy(db, "tx1") == {
        "wing": "infrastructure", "room": "watchdog",
        "origin_class": "first_party", "memory_class": "fact",
        "deprecated": 0, "superseded_by": None,
    }
    # room/origin_class optional — a wing-only row still resolves
    await memory.create_metadata(
        db, memory_id="tx2", created_at="2020-01-01T00:00:00+00:00", wing="memory",
    )
    assert await memory.get_taxonomy(db, "tx2") == {
        "wing": "memory", "room": None, "origin_class": None,
        "memory_class": "fact", "deprecated": 0, "superseded_by": None,
    }
    # an explicit non-default class is returned verbatim (authoritative for
    # the recovery worker — it must not be recomputed heuristically)
    await memory.create_metadata(
        db, memory_id="tx3", created_at="2020-01-01T00:00:00+00:00",
        wing="memory", memory_class="rule",
    )
    assert (await memory.get_taxonomy(db, "tx3"))["memory_class"] == "rule"
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
