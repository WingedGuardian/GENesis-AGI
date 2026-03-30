"""Tests for memory-mcp server — verify all tools are registered with correct signatures."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.mcp.memory_mcp import mcp


async def _get_tools():
    return await mcp.get_tools()


async def test_all_tools_registered():
    tools = await _get_tools()
    expected = [
        "memory_recall", "memory_store", "memory_extract", "memory_proactive",
        "memory_core_facts", "memory_stats",
        "observation_write", "observation_query", "observation_resolve",
        "evolution_propose",
        "conversation_history",
        "knowledge_recall", "knowledge_ingest", "knowledge_status",
    ]
    for name in expected:
        assert name in tools, f"Missing tool: {name}"


async def test_memory_recall_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["memory_recall"].fn(query="test")


async def test_observation_write_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["observation_write"].fn(content="test", source="test", type="test")


async def test_knowledge_recall_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["knowledge_recall"].fn(query="test")


async def test_knowledge_ingest_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["knowledge_ingest"].fn(content="test", project="p", domain="d")


async def test_evolution_propose_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["evolution_propose"].fn(
            proposal_type="test", current_content="a",
            proposed_change="b", rationale="c",
        )


async def test_procedure_store_registered():
    tools = await _get_tools()
    assert "procedure_store" in tools


async def test_procedure_recall_registered():
    tools = await _get_tools()
    assert "procedure_recall" in tools


# ─── End-to-end knowledge_ingest test ────────────────────────────────────────


async def test_knowledge_ingest_stores_with_correct_qdrant_id():
    """Verify knowledge_ingest writes qdrant_id that matches actual Qdrant point."""
    import genesis.mcp.memory_mcp as mod

    mock_store = AsyncMock()
    mock_store.store = AsyncMock(return_value="qdrant-uuid-123")
    mock_store._embeddings = MagicMock()
    mock_store._embeddings.model_name = "test-embed-model"

    mock_db = AsyncMock()
    mock_retriever = AsyncMock()
    mock_qdrant = MagicMock()

    old_store, old_db, old_retriever, old_qdrant = mod._store, mod._db, mod._retriever, mod._qdrant
    try:
        mod._store = mock_store
        mod._db = mock_db
        mod._retriever = mock_retriever
        mod._qdrant = mock_qdrant

        tools = await _get_tools()
        result = await tools["knowledge_ingest"].fn(
            content="VPC subnets are...",
            project="cloud-eng",
            domain="aws-vpc",
            authority="course",
        )

        assert result  # returns unit_id string

        # Verify MemoryStore.store was called with knowledge routing
        mock_store.store.assert_called_once()
        call_kwargs = mock_store.store.call_args
        assert call_kwargs[1]["memory_type"] == "knowledge"
        assert call_kwargs[1]["auto_link"] is False

        # Verify knowledge CRUD was called with the ACTUAL Qdrant ID
        with patch("genesis.mcp.memory_mcp.knowledge") as mock_crud:
            mock_crud.insert = AsyncMock(return_value="unit-456")
            await tools["knowledge_ingest"].fn(
                content="S3 buckets...",
                project="cloud",
                domain="aws-s3",
            )
            insert_kwargs = mock_crud.insert.call_args[1]
            # The qdrant_id must match what MemoryStore.store returned
            assert insert_kwargs["qdrant_id"] == "qdrant-uuid-123"
            assert insert_kwargs["embedding_model"] == "test-embed-model"
    finally:
        mod._store = old_store
        mod._db = old_db
        mod._retriever = old_retriever
        mod._qdrant = old_qdrant


async def test_evolution_propose_stores_pending():
    """Verify evolution_propose creates a pending proposal."""
    import genesis.mcp.memory_mcp as mod

    mock_db = AsyncMock()
    mock_store = AsyncMock()
    mock_retriever = AsyncMock()

    old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
    try:
        mod._store = mock_store
        mod._db = mock_db
        mod._retriever = mock_retriever

        with patch("genesis.mcp.memory_mcp.evolution_proposals") as mock_evo:
            mock_evo.create = AsyncMock(return_value="proposal-789")
            tools = await _get_tools()
            result = await tools["evolution_propose"].fn(
                proposal_type="soul_update",
                current_content="old text",
                proposed_change="new text",
                rationale="clarity",
            )
            assert result == "proposal-789"
            mock_evo.create.assert_called_once()
            call_kwargs = mock_evo.create.call_args[1]
            assert call_kwargs["proposal_type"] == "soul_update"
            assert call_kwargs["rationale"] == "clarity"
    finally:
        mod._store = old_store
        mod._db = old_db
        mod._retriever = old_retriever


# ─── conversation_history tool ────────────────────────────────────────────


async def test_conversation_history_registered():
    tools = await _get_tools()
    assert "conversation_history" in tools


async def test_conversation_history_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["conversation_history"].fn(channel="telegram")


async def test_conversation_history_telegram_returns_messages():
    """Verify conversation_history queries telegram_messages table."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod

    # Use a real in-memory db with the telegram_messages table
    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        # Insert test messages
        from genesis.db.crud.telegram_messages import store
        await store(
            real_db, chat_id=100, message_id=1, sender="user",
            content="hello", timestamp="2026-03-21T10:00:00",
        )
        await store(
            real_db, chat_id=100, message_id=-1, sender="genesis",
            content="hi there", timestamp="2026-03-21T10:00:01",
        )

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store = MagicMock()
            mod._db = real_db
            mod._retriever = MagicMock()

            tools = await _get_tools()
            result = await tools["conversation_history"].fn(
                channel="telegram", limit=10,
            )
            assert len(result) == 2
            assert result[0]["sender"] == "user"
            assert result[1]["sender"] == "genesis"
        finally:
            mod._store = old_store
            mod._db = old_db
            mod._retriever = old_retriever


async def test_conversation_history_search():
    """Verify conversation_history search filters correctly."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        from genesis.db.crud.telegram_messages import store
        await store(
            real_db, chat_id=100, message_id=1, sender="user",
            content="deploy the app", timestamp="2026-03-21T10:00:00",
        )
        await store(
            real_db, chat_id=100, message_id=2, sender="user",
            content="check the logs", timestamp="2026-03-21T10:00:01",
        )

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store = MagicMock()
            mod._db = real_db
            mod._retriever = MagicMock()

            tools = await _get_tools()
            result = await tools["conversation_history"].fn(
                channel="telegram", search="deploy",
            )
            assert len(result) == 1
            assert "deploy" in result[0]["content"]
        finally:
            mod._store = old_store
            mod._db = old_db
            mod._retriever = old_retriever


async def test_conversation_history_unknown_channel():
    """Unknown channel returns empty list."""
    import genesis.mcp.memory_mcp as mod

    old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
    try:
        mod._store = MagicMock()
        mod._db = MagicMock()
        mod._retriever = MagicMock()

        tools = await _get_tools()
        result = await tools["conversation_history"].fn(channel="slack")
        assert result == []
    finally:
        mod._store = old_store
        mod._db = old_db
        mod._retriever = old_retriever
