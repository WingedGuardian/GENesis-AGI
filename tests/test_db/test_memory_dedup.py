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
