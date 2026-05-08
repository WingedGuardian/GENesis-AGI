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
    return AsyncMock()


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


@pytest.fixture()
def store_no_linker(embedding_provider, qdrant, db):
    return MemoryStore(
        embedding_provider=embedding_provider,
        qdrant_client=qdrant,
        db=db,
        linker=None,
    )


@pytest.mark.asyncio()
async def test_store_returns_uuid(store):
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        result = await store.store("test content", "test-source")

    assert isinstance(result, str)
    assert len(result) == 36  # UUID format


@pytest.mark.asyncio()
async def test_store_calls_embed(store, embedding_provider):
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store("test content", "src")

    embedding_provider.embed.assert_awaited_once()


@pytest.mark.asyncio()
async def test_store_calls_qdrant_upsert(store):
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store("test content", "src", tags=["tag1"])

    mock_upsert.assert_called_once()
    call_kwargs = mock_upsert.call_args
    payload = call_kwargs.kwargs["payload"]
    assert payload["content"] == "test content"
    assert payload["source"] == "src"
    # store() auto-appends a class:{type} tag for FTS5 discoverability,
    # so we assert containment rather than strict equality
    assert "tag1" in payload["tags"]
    assert any(t.startswith("class:") for t in payload["tags"])
    assert payload["source_type"] == "memory"
    assert payload["retrieved_count"] == 0


@pytest.mark.asyncio()
async def test_store_calls_fts5_upsert(store):
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store("test content", "src")

    mock_mem.upsert.assert_awaited_once()
    call_kwargs = mock_mem.upsert.call_args
    assert call_kwargs.kwargs["content"] == "test content"
    assert call_kwargs.kwargs["collection"] == "episodic_memory"


@pytest.mark.asyncio()
async def test_store_auto_links(store, linker):
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store("test content", "src")

    linker.auto_link.assert_awaited_once()


@pytest.mark.asyncio()
async def test_store_skips_auto_link_when_disabled(store, linker):
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store("test content", "src", auto_link=False)

    linker.auto_link.assert_not_awaited()


@pytest.mark.asyncio()
async def test_store_works_without_linker(store_no_linker):
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        result = await store_no_linker.store("test content", "src")

    assert isinstance(result, str)


@pytest.mark.asyncio()
async def test_store_collection_override_bypasses_map(store):
    """Explicit collection param should bypass _COLLECTION_MAP."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store(
            "domain fact", "knowledge:test/domain",
            memory_type="knowledge",
            collection="knowledge_base",
        )

    # Qdrant upsert should target knowledge_base (override), not episodic_memory (map)
    call_kwargs = mock_upsert.call_args
    assert call_kwargs.kwargs["collection"] == "knowledge_base"
    # FTS5 should also use the override collection
    fts_kwargs = mock_mem.upsert.call_args
    assert fts_kwargs.kwargs["collection"] == "knowledge_base"


@pytest.mark.asyncio()
async def test_store_knowledge_type_defaults_to_knowledge_base(store):
    """memory_type='knowledge' without collection override routes to knowledge_base."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store("internal fact", "session_extraction", memory_type="knowledge")

    call_kwargs = mock_upsert.call_args
    assert call_kwargs.kwargs["collection"] == "knowledge_base"


@pytest.mark.asyncio()
async def test_store_scope_tag_user_by_default(store):
    """Default scope should be 'user' for episodic_memory entries."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store("conversation fact", "src")

    payload = mock_upsert.call_args.kwargs["payload"]
    assert payload["scope"] == "user"


@pytest.mark.asyncio()
async def test_store_scope_tag_external_for_knowledge_base(store):
    """Entries routed to knowledge_base should get scope='external'."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        await store.store(
            "domain fact", "knowledge:crypto/market",
            memory_type="knowledge",
            collection="knowledge_base",
        )

    payload = mock_upsert.call_args.kwargs["payload"]
    assert payload["scope"] == "external"


@pytest.mark.asyncio()
async def test_store_falls_back_on_qdrant_connection_error(store):
    """When Qdrant upsert raises a connection error, store should fall back to FTS5."""
    with patch("genesis.memory.store.upsert_point", side_effect=ConnectionError("refused")), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.pending_embeddings") as mock_pending:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_pending.create = AsyncMock(return_value="id")
        result = await store.store("test content", "src")

    assert isinstance(result, str)
    mock_mem.upsert.assert_awaited_once()  # FTS5 still written
    mock_pending.create.assert_awaited_once()  # Queued for later embedding


@pytest.mark.asyncio()
async def test_store_falls_back_on_unexpected_error(store):
    """Generic exceptions during vector storage should also fall back gracefully."""
    with patch("genesis.memory.store.upsert_point", side_effect=RuntimeError("kaboom")), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.pending_embeddings") as mock_pending:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_pending.create = AsyncMock(return_value="id")
        result = await store.store("test content", "src")

    assert isinstance(result, str)
    mock_mem.upsert.assert_awaited_once()
    mock_pending.create.assert_awaited_once()
