"""Tests for typed link creation in MemoryLinker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.linker import _VALID_LINK_TYPES, MemoryLinker


@pytest.fixture
def linker():
    """Create a MemoryLinker with mocked dependencies."""
    qdrant = MagicMock()
    db = AsyncMock()
    return MemoryLinker(qdrant_client=qdrant, db=db)


class TestValidLinkTypes:
    """Verify the set of valid link types matches schema."""

    def test_contains_original_types(self):
        assert "supports" in _VALID_LINK_TYPES
        assert "contradicts" in _VALID_LINK_TYPES
        assert "extends" in _VALID_LINK_TYPES
        assert "elaborates" in _VALID_LINK_TYPES

    def test_contains_new_typed_links(self):
        assert "discussed_in" in _VALID_LINK_TYPES
        assert "evaluated_for" in _VALID_LINK_TYPES
        assert "decided" in _VALID_LINK_TYPES
        assert "action_item_for" in _VALID_LINK_TYPES
        assert "categorized_as" in _VALID_LINK_TYPES
        assert "related_to" in _VALID_LINK_TYPES
        assert "succeeded_by" in _VALID_LINK_TYPES
        assert "preceded_by" in _VALID_LINK_TYPES

    def test_total_count(self):
        assert len(_VALID_LINK_TYPES) == 12


class TestCreateTypedLinks:
    """Tests for create_typed_links."""

    @pytest.mark.asyncio
    async def test_empty_relationships(self, linker):
        result = await linker.create_typed_links("mem-1", [])
        assert result == []

    @pytest.mark.asyncio
    async def test_invalid_link_type_skipped(self, linker):
        rels = [{"from": "A", "to": "B", "type": "nonexistent_type"}]
        result = await linker.create_typed_links("mem-1", rels)
        assert result == []

    @pytest.mark.asyncio
    async def test_missing_fields_skipped(self, linker):
        rels = [
            {"from": "A", "type": "related_to"},  # missing 'to'
            {"to": "B", "type": "related_to"},     # missing 'from' (but 'to' is what we use)
            {"from": "A", "to": "B"},              # missing 'type'
        ]
        result = await linker.create_typed_links("mem-1", rels)
        assert result == []

    @pytest.mark.asyncio
    @patch("genesis.memory.linker.memory_crud")
    @patch("genesis.memory.linker.memory_links")
    async def test_creates_link_on_fts5_match(self, mock_links, mock_crud, linker):
        mock_crud.search = AsyncMock(return_value=[
            {"memory_id": "target-1", "content": "Agentmail is a great service"},
        ])
        mock_links.create = AsyncMock()

        rels = [{"from": "User", "to": "Agentmail", "type": "evaluated_for"}]
        result = await linker.create_typed_links("mem-1", rels)

        assert len(result) == 1
        assert result[0].source_id == "mem-1"
        assert result[0].target_id == "target-1"
        assert result[0].link_type == "evaluated_for"
        mock_links.create.assert_called_once()

    @pytest.mark.asyncio
    @patch("genesis.memory.linker.memory_crud")
    async def test_no_match_returns_empty(self, mock_crud, linker):
        mock_crud.search = AsyncMock(return_value=[])

        rels = [{"from": "A", "to": "NonexistentEntity", "type": "related_to"}]
        result = await linker.create_typed_links("mem-1", rels)
        assert result == []

    @pytest.mark.asyncio
    @patch("genesis.memory.linker.memory_crud")
    async def test_self_link_skipped(self, mock_crud, linker):
        mock_crud.search = AsyncMock(return_value=[
            {"memory_id": "mem-1", "content": "Self reference"},
        ])

        rels = [{"from": "A", "to": "Self", "type": "related_to"}]
        result = await linker.create_typed_links("mem-1", rels)
        assert result == []

    @pytest.mark.asyncio
    @patch("genesis.memory.linker.memory_crud")
    @patch("genesis.memory.linker.memory_links")
    async def test_duplicate_link_handled(self, mock_links, mock_crud, linker):
        mock_crud.search = AsyncMock(return_value=[
            {"memory_id": "target-1", "content": "Some content"},
        ])
        # Simulate IntegrityError on duplicate
        mock_links.create = AsyncMock(side_effect=Exception("UNIQUE constraint failed"))

        rels = [{"from": "A", "to": "B", "type": "related_to"}]
        result = await linker.create_typed_links("mem-1", rels)
        # Duplicate silently handled — no link returned
        assert result == []

    @pytest.mark.asyncio
    @patch("genesis.memory.linker.memory_crud")
    @patch("genesis.memory.linker.memory_links")
    async def test_prefers_exact_substring_match(self, mock_links, mock_crud, linker):
        mock_crud.search = AsyncMock(return_value=[
            {"memory_id": "t-1", "content": "Something about agents"},
            {"memory_id": "t-2", "content": "Agentmail service details"},
        ])
        mock_links.create = AsyncMock()

        rels = [{"from": "User", "to": "Agentmail", "type": "evaluated_for"}]
        result = await linker.create_typed_links("mem-1", rels)

        assert len(result) == 1
        # Should prefer t-2 because "Agentmail" is an exact substring
        assert result[0].target_id == "t-2"
