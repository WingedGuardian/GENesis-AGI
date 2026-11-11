"""Tests for memory dedup: find_exact_duplicate CRUD function."""

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


async def test_find_exact_duplicate_no_match(db):
    """Returns None when content doesn't exist."""
    result = await memory.find_exact_duplicate(db, content="nonexistent content")
    assert result is None


async def test_find_exact_duplicate_finds_match(db):
    """Returns memory_id when exact content exists."""
    await memory.create(db, memory_id="dup-m1", content="hello world duplicate test")
    result = await memory.find_exact_duplicate(
        db, content="hello world duplicate test",
    )
    assert result == "dup-m1"


async def test_find_exact_duplicate_cross_collection(db):
    """Finds duplicate regardless of FTS collection column value.

    This is critical: the FTS collection column is unreliable (uniformly
    episodic_memory), so dedup must be collection-agnostic.
    """
    await memory.create(
        db, memory_id="kb-m1", content="knowledge content",
        collection="knowledge_base",
    )
    # Search without specifying collection — should still find it
    result = await memory.find_exact_duplicate(db, content="knowledge content")
    assert result == "kb-m1"


async def test_find_exact_duplicate_empty_content(db):
    """Returns None for empty content."""
    result = await memory.find_exact_duplicate(db, content="")
    assert result is None


async def test_find_exact_duplicate_similar_but_different(db):
    """Does not match content that shares prefix but differs."""
    await memory.create(
        db, memory_id="sim-m1",
        content="this is the original content with specific ending AAA",
    )
    # Same prefix, different ending
    result = await memory.find_exact_duplicate(
        db, content="this is the original content with specific ending BBB",
    )
    assert result is None


async def test_find_exact_duplicate_same_length_different_content(db):
    """Does not match content with same length but different text."""
    content_a = "aaaa bbbb cccc"
    content_b = "xxxx yyyy zzzz"
    assert len(content_a) == len(content_b)

    await memory.create(db, memory_id="len-m1", content=content_a)
    result = await memory.find_exact_duplicate(db, content=content_b)
    assert result is None


async def test_find_exact_duplicate_returns_first_match(db):
    """When multiple duplicates exist, returns the first one found."""
    await memory.create(db, memory_id="first-m1", content="duplicate content here")
    # Create a second entry with same content but different ID
    # (simulating the bug this fixes)
    await memory.create(db, memory_id="second-m2", content="duplicate content here")

    result = await memory.find_exact_duplicate(
        db, content="duplicate content here",
    )
    # Should return one of them (which one depends on rowid order)
    assert result in ("first-m1", "second-m2")


@pytest.mark.parametrize("scope,stored,metadata,expected", [
    (None, None, True, True), (None, None, False, True),
    (None, "worker", True, False), ("worker", "worker", True, True),
    ("worker", "other", True, False), ("worker", None, True, False),
    ("worker", None, False, False), ("", "", True, True), ("", None, True, False),
])
async def test_duplicate_visibility(db, scope, stored, metadata, expected):
    await memory.create(db, memory_id="scoped", content="scope fixture")
    if metadata:
        await memory.create_metadata(db, memory_id="scoped", created_at="2020-01-01T00:00:00+00:00", source_subsystem=stored)
    assert await memory.find_exact_duplicate(db, content="scope fixture", source_subsystem=scope) == ("scoped" if expected else None)


@pytest.mark.parametrize("field,value,expected", [
    ("deprecated", 1, False), ("deprecated", 0, True),
    ("invalid_at", "2000-01-01T00:00:00+00:00", False),
    ("invalid_at", "9999-01-01T00:00:00+00:00", True),
    ("expires_at", "2000-01-01T00:00:00+00:00", True),
])
async def test_duplicate_recall_lifecycle(db, field, value, expected):
    await memory.create(db, memory_id="lifecycle", content="lifecycle fixture")
    await memory.create_metadata(db, memory_id="lifecycle", created_at="2020-01-01T00:00:00+00:00")
    await db.execute(f"UPDATE memory_metadata SET {field} = ? WHERE memory_id = ?", (value, "lifecycle"))
    assert await memory.find_exact_duplicate(db, content="lifecycle fixture") == ("lifecycle" if expected else None)


@pytest.mark.parametrize("status,exclude", [("pending", True), ("processing", True), ("completed", False), ("failed", False)])
async def test_duplicate_uses_canonical_delete_intent(db, status, exclude):
    from genesis.memory.delete_tombstones import enqueue_tombstone

    await memory.create(db, memory_id="deleting", content="delete fixture")
    assert await enqueue_tombstone(db, memory_id="deleting", reason="synthetic")
    await db.execute("UPDATE deferred_work_queue SET status = ?", (status,))
    assert await memory.find_exact_duplicate(db, content="delete fixture") == (None if exclude else "deleting")


@pytest.mark.parametrize("payload,exclude", [
    ('{"topic":"candidate","category":"memory_delete"}', True),
    ('{"topic":"candidate","category":"memory_delete","signal_type":""}', True),
    ('{"topic":"candidate","category":"other"}', False),
    ('{"topic":"candidate","category":"memory_delete","signal_type":"other"}', False),
    ('{"memory_id":"candidate"}', False), ('invalid json', False),
])
async def test_duplicate_delete_identity_and_corrupt_payload(db, payload, exclude):
    from genesis.memory.delete_tombstones import enqueue_tombstone, has_open_tombstone

    await memory.create(db, memory_id="candidate", content="identity fixture")
    assert await enqueue_tombstone(db, memory_id="candidate", reason="synthetic")
    await db.execute("UPDATE deferred_work_queue SET payload_json = ?", (payload,))
    assert await has_open_tombstone(db, memory_id="candidate") is exclude
    assert await memory.find_exact_duplicate(db, content="identity fixture") == (None if exclude else "candidate")


async def test_duplicate_filters_before_candidate_limit(db):
    for i in range(205):
        mid = f"hidden-{i}"
        await memory.create(db, memory_id=mid, content="repeated content")
        await memory.create_metadata(db, memory_id=mid, created_at="2020-01-01T00:00:00+00:00", source_subsystem="worker")
    await memory.create(db, memory_id="visible", content="repeated content")
    assert await memory.find_exact_duplicate(db, content="repeated content") == "visible"


async def test_duplicate_exact_identity_before_candidate_limit(db):
    for i in range(205):
        await memory.create(db, memory_id=f"collision-{i}", content="x" * 200 + f"{i:03}")
    target = "x" * 200 + "end"
    await memory.create(db, memory_id="exact", content=target)
    assert await memory.find_exact_duplicate(db, content=target) == "exact"


@pytest.mark.parametrize("text", ["élan 東京", "a\x00b", "a\nb"])
async def test_duplicate_full_text_identity(db, text):
    await memory.create(db, memory_id="exact", content=text)
    assert await memory.find_exact_duplicate(db, content=text) == "exact"
