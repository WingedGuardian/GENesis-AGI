"""Tests for session_bookmarks CRUD operations."""

from __future__ import annotations

import uuid

import aiosqlite
import pytest

from genesis.db.crud import session_bookmarks as crud
from genesis.db.schema import create_all_tables, seed_data


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await seed_data(conn)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_create_and_get_by_id(db):
    bid = str(uuid.uuid4())
    await crud.create(
        db, id=bid, cc_session_id="sess-1", bookmark_type="micro",
        topic="Test topic", created_at="2026-03-22T10:00:00",
    )
    row = await crud.get_by_id(db, bid)
    assert row is not None
    assert row["topic"] == "Test topic"
    assert row["bookmark_type"] == "micro"
    assert row["has_rich_summary"] == 0


@pytest.mark.asyncio
async def test_get_by_session(db):
    bid = str(uuid.uuid4())
    await crud.create(
        db, id=bid, cc_session_id="sess-abc",
        bookmark_type="topic", topic="ABC session",
        created_at="2026-03-22T10:00:00",
    )
    row = await crud.get_by_session(db, "sess-abc")
    assert row is not None
    assert row["id"] == bid


@pytest.mark.asyncio
async def test_get_by_session_not_found(db):
    row = await crud.get_by_session(db, "nonexistent")
    assert row is None


@pytest.mark.asyncio
async def test_get_recent(db):
    for i in range(3):
        await crud.create(
            db, id=str(uuid.uuid4()), cc_session_id=f"sess-{i}",
            bookmark_type="micro", topic=f"Topic {i}",
            created_at=f"2026-03-{20 + i}T10:00:00",
        )
    rows = await crud.get_recent(db, limit=2)
    assert len(rows) == 2
    assert rows[0]["topic"] == "Topic 2"  # Most recent first


@pytest.mark.asyncio
async def test_mark_enriched(db):
    bid = str(uuid.uuid4())
    await crud.create(
        db, id=bid, cc_session_id="sess-enrich",
        bookmark_type="micro", topic="Enrich me",
        created_at="2026-03-22T10:00:00",
    )
    await crud.mark_enriched(db, bid)
    row = await crud.get_by_id(db, bid)
    assert row["has_rich_summary"] == 1
    assert row["enriched_at"] is not None


@pytest.mark.asyncio
async def test_increment_resumed(db):
    bid = str(uuid.uuid4())
    await crud.create(
        db, id=bid, cc_session_id="sess-resume",
        bookmark_type="micro", topic="Resume me",
        created_at="2026-03-22T10:00:00",
    )
    await crud.increment_resumed(db, bid)
    await crud.increment_resumed(db, bid)
    row = await crud.get_by_id(db, bid)
    assert row["resumed_count"] == 2
    assert row["last_resumed_at"] is not None


@pytest.mark.asyncio
async def test_insert_or_ignore_duplicate(db):
    bid = str(uuid.uuid4())
    await crud.create(
        db, id=bid, cc_session_id="sess-dup",
        bookmark_type="micro", topic="First",
        created_at="2026-03-22T10:00:00",
    )
    # Second insert with same ID is ignored
    result = await crud.create(
        db, id=bid, cc_session_id="sess-dup",
        bookmark_type="micro", topic="Second",
        created_at="2026-03-22T11:00:00",
    )
    assert result is False  # Deduped
    row = await crud.get_by_id(db, bid)
    assert row["topic"] == "First"


@pytest.mark.asyncio
async def test_source_column_defaults(db):
    """Source defaults to 'auto' when not specified."""
    bid = str(uuid.uuid4())
    await crud.create(
        db, id=bid, cc_session_id="sess-src-default",
        bookmark_type="micro", topic="Default source",
        created_at="2026-03-22T10:00:00",
    )
    row = await crud.get_by_id(db, bid)
    assert row["source"] == "auto"


@pytest.mark.asyncio
async def test_source_explicit(db):
    """Source can be set to 'explicit'."""
    bid = str(uuid.uuid4())
    await crud.create(
        db, id=bid, cc_session_id="sess-src-explicit",
        bookmark_type="micro", topic="Explicit source",
        created_at="2026-03-22T10:00:00",
        source="explicit",
    )
    row = await crud.get_by_id(db, bid)
    assert row["source"] == "explicit"


@pytest.mark.asyncio
async def test_dedup_same_session_same_source(db):
    """Second insert with same (cc_session_id, source) is skipped."""
    bid1 = str(uuid.uuid4())
    result1 = await crud.create(
        db, id=bid1, cc_session_id="sess-dedup",
        bookmark_type="micro", topic="First",
        created_at="2026-03-22T10:00:00",
        source="auto",
    )
    assert result1 is True

    bid2 = str(uuid.uuid4())
    result2 = await crud.create(
        db, id=bid2, cc_session_id="sess-dedup",
        bookmark_type="micro", topic="Second",
        created_at="2026-03-22T11:00:00",
        source="auto",
    )
    assert result2 is False  # Deduped


@pytest.mark.asyncio
async def test_different_sources_allowed(db):
    """Same session can have one auto + one explicit bookmark."""
    bid1 = str(uuid.uuid4())
    result1 = await crud.create(
        db, id=bid1, cc_session_id="sess-multi-src",
        bookmark_type="micro", topic="Auto bookmark",
        created_at="2026-03-22T10:00:00",
        source="auto",
    )
    assert result1 is True

    bid2 = str(uuid.uuid4())
    result2 = await crud.create(
        db, id=bid2, cc_session_id="sess-multi-src",
        bookmark_type="micro", topic="Explicit bookmark",
        created_at="2026-03-22T11:00:00",
        source="explicit",
    )
    assert result2 is True  # Different source, allowed


@pytest.mark.asyncio
async def test_get_recent_with_source_filter(db):
    """get_recent can filter by source."""
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-filter-1",
        bookmark_type="micro", topic="Auto",
        created_at="2026-03-22T10:00:00", source="auto",
    )
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-filter-2",
        bookmark_type="micro", topic="Explicit",
        created_at="2026-03-22T11:00:00", source="explicit",
    )
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-filter-3",
        bookmark_type="micro", topic="Plan",
        created_at="2026-03-22T12:00:00", source="plan",
    )

    explicit_only = await crud.get_recent(db, limit=10, source="explicit")
    assert len(explicit_only) == 1
    assert explicit_only[0]["topic"] == "Explicit"

    all_bookmarks = await crud.get_recent(db, limit=10)
    assert len(all_bookmarks) == 3


@pytest.mark.asyncio
async def test_search_by_topic(db):
    """Search finds bookmarks by topic keyword."""
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-search-1",
        bookmark_type="micro", topic="Voice Interface PRD",
        created_at="2026-03-22T10:00:00",
    )
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-search-2",
        bookmark_type="micro", topic="Portfolio deployment plan",
        created_at="2026-03-22T11:00:00",
    )
    results = await crud.search(db, "voice", limit=10)
    assert len(results) == 1
    assert results[0]["topic"] == "Voice Interface PRD"


@pytest.mark.asyncio
async def test_search_by_tags(db):
    """Search finds bookmarks by tag content."""
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-tag-1",
        bookmark_type="micro", topic="Plan session",
        tags='["plan", "approved", "voice", "ambient"]',
        created_at="2026-03-22T10:00:00",
    )
    results = await crud.search(db, "ambient", limit=10)
    assert len(results) == 1
    assert results[0]["cc_session_id"] == "sess-tag-1"


@pytest.mark.asyncio
async def test_search_multiple_terms_and(db):
    """Multiple search terms are ANDed — all must match."""
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-and-1",
        bookmark_type="micro", topic="Voice Interface PRD",
        tags='["voice", "ambient"]',
        created_at="2026-03-22T10:00:00",
    )
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-and-2",
        bookmark_type="micro", topic="Portfolio plan",
        tags='["portfolio"]',
        created_at="2026-03-22T11:00:00",
    )
    # Both terms must match
    results = await crud.search(db, "voice PRD", limit=10)
    assert len(results) == 1
    assert results[0]["cc_session_id"] == "sess-and-1"

    # "voice portfolio" matches neither fully
    results = await crud.search(db, "voice portfolio", limit=10)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_search_empty_query_returns_recent(db):
    """Empty query falls back to get_recent."""
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-empty-1",
        bookmark_type="micro", topic="First",
        created_at="2026-03-22T10:00:00",
    )
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-empty-2",
        bookmark_type="micro", topic="Second",
        created_at="2026-03-22T11:00:00",
    )
    results = await crud.search(db, "", limit=10)
    assert len(results) == 2
    assert results[0]["topic"] == "Second"  # Most recent first


@pytest.mark.asyncio
async def test_search_no_matches(db):
    """Search returns empty list for non-matching query."""
    await crud.create(
        db, id=str(uuid.uuid4()), cc_session_id="sess-no-match",
        bookmark_type="micro", topic="Ego dispatch fix",
        created_at="2026-03-22T10:00:00",
    )
    results = await crud.search(db, "nonexistent_gibberish", limit=10)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_search_respects_limit(db):
    """Search respects the limit parameter."""
    for i in range(5):
        await crud.create(
            db, id=str(uuid.uuid4()), cc_session_id=f"sess-limit-{i}",
            bookmark_type="micro", topic=f"Plan session {i}",
            tags='["plan"]',
            created_at=f"2026-03-{20 + i}T10:00:00",
        )
    results = await crud.search(db, "plan", limit=3)
    assert len(results) == 3
