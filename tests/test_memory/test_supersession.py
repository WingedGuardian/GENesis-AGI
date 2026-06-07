"""Tests for memory supersession — store with supersedes, mark_superseded, retrieval filtering."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.store import MemoryStore


@pytest.fixture()
def embedding_provider():
    ep = MagicMock()
    ep.embed = AsyncMock(return_value=[0.1] * 1024)
    ep.enrich = MagicMock(return_value="episodic: test content")
    return ep


@pytest.fixture()
def qdrant():
    return MagicMock()


@pytest.fixture()
def db():
    mock = AsyncMock()
    # Default: execute returns a cursor with fetchone returning None
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=None)
    mock.execute = AsyncMock(return_value=cursor)
    return mock


@pytest.fixture()
def linker():
    lnk = MagicMock()
    lnk.auto_link = AsyncMock(return_value=[])
    return lnk


@pytest.fixture()
def store(embedding_provider, qdrant, db, linker):
    return MemoryStore(
        embedding_provider=embedding_provider,
        qdrant_client=qdrant,
        db=db,
        linker=linker,
    )


@pytest.mark.asyncio()
async def test_store_with_supersedes_marks_old_deprecated(store, db):
    """Storing with supersedes should mark the old memory as deprecated in SQLite."""
    old_id = "old-memory-id"

    # Mock the metadata lookup for the old memory (embedded, episodic_memory)
    metadata_cursor = AsyncMock()
    metadata_cursor.fetchone = AsyncMock(return_value=("episodic_memory", "embedded"))

    # We need to track all execute calls
    call_results = []
    original_execute = db.execute

    async def track_execute(sql, params=None):
        result = await original_execute(sql, params)
        call_results.append((sql, params))
        # Return the metadata cursor for the SELECT query
        if isinstance(sql, str) and "SELECT collection" in sql:
            return metadata_cursor
        return result

    db.execute = AsyncMock(side_effect=track_execute)

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "embedded", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(return_value=(old_id, "new"))

        new_id = await store.store(
            "CC upgraded to v2.1.154",
            "conversation",
            supersedes=old_id,
        )

    assert isinstance(new_id, str)
    assert len(new_id) == 36

    # Verify mark_superseded was called with old_id and new_id
    mock_mem.mark_superseded.assert_awaited_once()
    call_args = mock_mem.mark_superseded.call_args
    assert call_args[0][1] == old_id  # old_id
    assert call_args[0][2] == new_id  # new_id


@pytest.mark.asyncio()
async def test_store_with_supersedes_updates_qdrant(store, db):
    """Storing with supersedes should update the old memory's Qdrant payload."""
    old_id = "old-memory-id"

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload") as mock_update, \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "embedded", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(return_value=(old_id, "new"))

        new_id = await store.store(
            "new fact",
            "conversation",
            supersedes=old_id,
        )

    # Verify Qdrant update_payload was called
    mock_update.assert_called_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs.kwargs["collection"] == "episodic_memory"
    assert call_kwargs.kwargs["point_id"] == old_id
    assert call_kwargs.kwargs["payload"]["deprecated"] is True
    assert call_kwargs.kwargs["payload"]["merged_into"] == new_id


@pytest.mark.asyncio()
async def test_store_with_supersedes_skips_qdrant_for_fts5_only(store, db):
    """FTS5-only memories should not get a Qdrant update_payload call."""
    old_id = "fts5-only-memory"

    metadata_cursor = AsyncMock()
    metadata_cursor.fetchone = AsyncMock(return_value=("episodic_memory", "fts5_only"))

    async def route_execute(sql, params=None):
        if isinstance(sql, str) and "SELECT collection" in sql:
            return metadata_cursor
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        return cursor

    db.execute = AsyncMock(side_effect=route_execute)

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload") as mock_update, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)

        await store.store("new fact", "conversation", supersedes=old_id)

    # Qdrant update_payload should NOT be called for fts5_only memories
    mock_update.assert_not_called()


@pytest.mark.asyncio()
async def test_store_with_supersedes_creates_succeeded_by_link(store, db):
    """Supersession should create a succeeded_by link in memory_links."""
    old_id = "old-memory-id"

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "embedded", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(return_value=(old_id, "new"))

        new_id = await store.store("new fact", "conversation", supersedes=old_id)

    mock_links.create.assert_awaited_once()
    link_kwargs = mock_links.create.call_args.kwargs
    assert link_kwargs["source_id"] == old_id
    assert link_kwargs["target_id"] == new_id
    assert link_kwargs["link_type"] == "succeeded_by"
    assert link_kwargs["strength"] == 1.0


@pytest.mark.asyncio()
async def test_store_without_supersedes_skips_deprecation(store, db):
    """Normal store (no supersedes) should not trigger any deprecation logic."""
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload") as mock_update, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)

        await store.store("normal content", "conversation")

    mock_update.assert_not_called()


@pytest.mark.asyncio()
async def test_supersedes_failure_does_not_block_store(store, db):
    """If supersession fails, the new memory should still be stored successfully."""
    old_id = "nonexistent-id"

    # Make the metadata lookup fail
    async def failing_execute(sql, params=None):
        if isinstance(sql, str) and "deprecated = 1" in sql:
            raise RuntimeError("simulated DB error")
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        return cursor

    db.execute = AsyncMock(side_effect=failing_execute)

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)

        result = await store.store(
            "new content",
            "conversation",
            supersedes=old_id,
        )

    # Store should succeed despite supersession failure
    assert isinstance(result, str)
    assert len(result) == 36


@pytest.mark.asyncio()
async def test_search_ranked_excludes_deprecated_by_default():
    """search_ranked should exclude deprecated memories by default."""
    import aiosqlite

    db = AsyncMock(spec=aiosqlite.Connection)
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor)

    from genesis.db.crud.memory import search_ranked
    await search_ranked(db, query="test query")

    # Verify the SQL includes the deprecated filter
    sql = db.execute.call_args[0][0]
    assert "deprecated" in sql
    assert "deprecated = 0" in sql or "deprecated IS NULL" in sql


@pytest.mark.asyncio()
async def test_search_ranked_includes_deprecated_when_requested():
    """search_ranked with include_deprecated=True should not filter deprecated."""
    import aiosqlite

    db = AsyncMock(spec=aiosqlite.Connection)
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor)

    from genesis.db.crud.memory import search_ranked
    await search_ranked(db, query="test query", include_deprecated=True)

    # Verify the SQL does NOT include the deprecated filter
    sql = db.execute.call_args[0][0]
    assert "deprecated = 0" not in sql


@pytest.mark.asyncio()
async def test_qdrant_search_excludes_deprecated_by_default():
    """Qdrant search() should include deprecated must_not filter by default."""
    from unittest.mock import MagicMock

    from genesis.qdrant.collections import search

    client = MagicMock()
    client.query_points = MagicMock(return_value=MagicMock(points=[]))

    search(
        client,
        collection="episodic_memory",
        query_vector=[0.1] * 1024,
        limit=5,
    )

    # Verify the filter includes deprecated must_not
    call_kwargs = client.query_points.call_args.kwargs
    query_filter = call_kwargs["query_filter"]
    must_not = query_filter.must_not
    assert must_not is not None
    assert any(
        getattr(cond, "key", None) == "deprecated"
        for cond in must_not
    )


@pytest.mark.asyncio()
async def test_qdrant_search_skips_deprecated_filter_when_included():
    """Qdrant search() with include_deprecated=True should not filter deprecated."""
    from unittest.mock import MagicMock

    from genesis.qdrant.collections import search

    client = MagicMock()
    client.query_points = MagicMock(return_value=MagicMock(points=[]))

    search(
        client,
        collection="episodic_memory",
        query_vector=[0.1] * 1024,
        limit=5,
        include_deprecated=True,
    )

    # Verify the filter does NOT include deprecated must_not
    call_kwargs = client.query_points.call_args.kwargs
    query_filter = call_kwargs["query_filter"]
    must_not = query_filter.must_not
    # must_not should be None or empty (no deprecated filter)
    if must_not:
        assert not any(
            getattr(cond, "key", None) == "deprecated"
            for cond in must_not
        )
