"""Tests for BookmarkManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.bookmark.manager import BookmarkManager, BookmarkResult
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


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.store = AsyncMock(return_value="mem-123")
    return store


@pytest.fixture
def mock_retriever():
    return AsyncMock()


@pytest.fixture
def mgr(mock_store, mock_retriever, db):
    return BookmarkManager(
        memory_store=mock_store,
        hybrid_retriever=mock_retriever,
        db=db,
    )


@pytest.mark.asyncio
async def test_create_micro(mgr, mock_store):
    messages = [
        {"text": "Let's discuss the API design", "timestamp": "2026-03-22T10:00:00"},
        {"text": "We should use REST not GraphQL", "timestamp": "2026-03-22T10:05:00"},
    ]
    bid = await mgr.create_micro(
        cc_session_id="sess-test-123",
        context_messages=messages,
    )
    assert bid  # UUID string
    assert len(bid) == 36

    # Verify memory was stored
    mock_store.store.assert_called_once()
    call_kwargs = mock_store.store.call_args.kwargs
    assert call_kwargs["memory_type"] == "session_bookmark"
    assert call_kwargs["source"] == "session:sess-test-123"
    assert "session_bookmark" in call_kwargs["tags"]


@pytest.mark.asyncio
async def test_create_topic(mgr, mock_store):
    messages = [{"text": "Mid-session topic snapshot", "timestamp": ""}]
    bid = await mgr.create_topic(
        cc_session_id="sess-topic-456",
        context_messages=messages,
        tags=["marketing"],
    )
    assert bid
    call_kwargs = mock_store.store.call_args.kwargs
    assert "topic" in call_kwargs["tags"]
    assert "marketing" in call_kwargs["tags"]


@pytest.mark.asyncio
async def test_create_explicit(mgr, mock_store, db):
    """Explicit bookmarks use context_note as topic."""
    messages = [
        {"text": "Let's discuss the API design", "timestamp": "2026-03-22T10:00:00"},
    ]
    bid = await mgr.create_explicit(
        cc_session_id="sess-explicit-1",
        context_messages=messages,
        context_note="Building MCP server for memory tools",
        tags=["mcp", "memory"],
    )
    assert bid
    assert len(bid) == 36

    # Verify memory stored with higher confidence
    call_kwargs = mock_store.store.call_args.kwargs
    assert call_kwargs["confidence"] == 0.90
    assert "MCP" in call_kwargs["content"] or "mcp" in call_kwargs["tags"]

    # Verify index table has source=explicit
    from genesis.db.crud import session_bookmarks as crud
    row = await crud.get_by_session(db, "sess-explicit-1")
    assert row is not None
    assert row["source"] == "explicit"
    assert row["topic"] == "Building MCP server for memory tools"


@pytest.mark.asyncio
async def test_create_explicit_no_context(mgr, mock_store, db):
    """Explicit bookmark without context_note falls back to heuristic topic."""
    messages = [
        {"text": "hi", "timestamp": ""},
        {"text": "Working on the authentication redesign", "timestamp": ""},
    ]
    bid = await mgr.create_explicit(
        cc_session_id="sess-explicit-2",
        context_messages=messages,
    )
    assert bid

    from genesis.db.crud import session_bookmarks as crud
    row = await crud.get_by_session(db, "sess-explicit-2")
    assert row["source"] == "explicit"
    assert "authentication" in row["topic"].lower()


@pytest.mark.asyncio
async def test_create_micro_with_source(mgr, db):
    """create_micro accepts and stores source parameter."""
    messages = [{"text": "Plan approved: some plan", "timestamp": ""}]
    bid = await mgr.create_micro(
        cc_session_id="sess-plan-1",
        context_messages=messages,
        source="plan",
    )
    assert bid

    from genesis.db.crud import session_bookmarks as crud
    row = await crud.get_by_session(db, "sess-plan-1")
    assert row["source"] == "plan"


@pytest.mark.asyncio
async def test_recent_includes_source(mgr, db):
    """Recent results include source field."""
    await mgr.create_explicit(
        cc_session_id="sess-src-1",
        context_messages=[{"text": "Explicit session", "timestamp": ""}],
        context_note="Test explicit",
    )
    results = await mgr.recent(limit=5)
    assert len(results) >= 1
    assert results[0].source == "explicit"


@pytest.mark.asyncio
async def test_extract_topic_from_messages(mgr):
    """extract_topic prefers the LAST substantive message."""
    messages = [
        {"text": "hi", "timestamp": ""},
        {"text": "Let's redesign the authentication flow for better security", "timestamp": ""},
    ]
    topic = mgr._extract_topic(messages)
    assert "authentication" in topic.lower()


@pytest.mark.asyncio
async def test_extract_topic_prefers_last(mgr):
    """extract_topic takes the last message, not the first."""
    messages = [
        {"text": "Let's start with the database schema", "timestamp": ""},
        {"text": "Actually we need to fix the MCP server first", "timestamp": ""},
    ]
    topic = mgr._extract_topic(messages)
    assert "MCP" in topic


@pytest.mark.asyncio
async def test_extract_topic_empty(mgr):
    assert mgr._extract_topic([]) == "Untitled session"


@pytest.mark.asyncio
async def test_extract_topic_short_messages(mgr):
    messages = [{"text": "hi", "timestamp": ""}, {"text": "ok", "timestamp": ""}]
    assert mgr._extract_topic(messages) == "Untitled session"


@pytest.mark.asyncio
async def test_enrich(mgr, mock_store, db):
    # Create a micro-bookmark first
    messages = [{"text": "Working on bookmark system", "timestamp": ""}]
    bid = await mgr.create_micro(
        cc_session_id="sess-enrich",
        context_messages=messages,
    )

    # Now enrich it
    mock_store.store.reset_mock()
    result = await mgr.enrich(bid, "Rich summary: key decisions about bookmarks")
    assert result is True
    mock_store.store.assert_called_once()
    assert "enriched" in mock_store.store.call_args.kwargs["tags"]


@pytest.mark.asyncio
async def test_enrich_nonexistent(mgr):
    result = await mgr.enrich("nonexistent-id", "Summary")
    assert result is False


@pytest.mark.asyncio
async def test_recent(mgr, mock_store):
    for i in range(3):
        await mgr.create_micro(
            cc_session_id=f"sess-recent-{i}",
            context_messages=[{"text": f"Session {i} content", "timestamp": ""}],
        )
    results = await mgr.recent(limit=2)
    assert len(results) == 2
    assert all(isinstance(r, BookmarkResult) for r in results)


@pytest.mark.asyncio
async def test_search_filters_by_type(mgr, mock_retriever):
    """Search should only return session_bookmark type memories."""
    # Mock retriever returns mixed types
    mock_result_bookmark = MagicMock()
    mock_result_bookmark.memory_type = "session_bookmark"
    mock_result_bookmark.source = "session:sess-123"
    mock_result_bookmark.memory_id = "mem-1"
    mock_result_bookmark.score = 0.9
    mock_result_bookmark.content = "Bookmark content"

    mock_result_episodic = MagicMock()
    mock_result_episodic.memory_type = "episodic"
    mock_result_episodic.source = "conversation"
    mock_result_episodic.memory_id = "mem-2"
    mock_result_episodic.score = 0.8
    mock_result_episodic.content = "Regular memory"

    mock_retriever.recall = AsyncMock(
        return_value=[mock_result_bookmark, mock_result_episodic],
    )

    results = await mgr.search("test query")
    # Only the bookmark should be returned
    assert len(results) == 1
    assert results[0].cc_session_id == "sess-123"
