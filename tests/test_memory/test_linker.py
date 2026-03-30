from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.linker import MemoryLinker
from genesis.memory.types import LinkRecord


@pytest.fixture()
def qdrant():
    return MagicMock()


@pytest.fixture()
def db():
    return AsyncMock()


@pytest.fixture()
def linker(qdrant, db):
    return MemoryLinker(qdrant_client=qdrant, db=db)


def _hit(point_id: str, score: float) -> dict:
    return {"id": point_id, "score": score, "payload": {}}


@pytest.mark.asyncio()
async def test_auto_link_creates_links(linker):
    with patch("genesis.memory.linker.search", return_value=[
        _hit("other-1", 0.85),
        _hit("other-2", 0.80),
    ]), patch("genesis.memory.linker.memory_links") as mock_crud:
        mock_crud.create = AsyncMock(return_value=("src", "tgt"))
        links = await linker.auto_link("mem-1", [0.1] * 1024)

    assert len(links) == 2
    assert all(isinstance(link, LinkRecord) for link in links)
    assert links[0].target_id == "other-1"
    assert links[1].target_id == "other-2"


@pytest.mark.asyncio()
async def test_auto_link_filters_self(linker):
    with patch("genesis.memory.linker.search", return_value=[
        _hit("mem-1", 0.99),
        _hit("other-1", 0.85),
    ]), patch("genesis.memory.linker.memory_links") as mock_crud:
        mock_crud.create = AsyncMock(return_value=("src", "tgt"))
        links = await linker.auto_link("mem-1", [0.1] * 1024)

    assert len(links) == 1
    assert links[0].target_id == "other-1"


@pytest.mark.asyncio()
async def test_auto_link_filters_below_threshold(linker):
    with patch("genesis.memory.linker.search", return_value=[
        _hit("other-1", 0.74),
        _hit("other-2", 0.50),
    ]), patch("genesis.memory.linker.memory_links") as mock_crud:
        mock_crud.create = AsyncMock(return_value=("src", "tgt"))
        links = await linker.auto_link("mem-1", [0.1] * 1024)

    assert len(links) == 0


@pytest.mark.asyncio()
async def test_auto_link_classifies_extends_vs_supports(linker):
    with patch("genesis.memory.linker.search", return_value=[
        _hit("high", 0.95),
        _hit("mid", 0.80),
    ]), patch("genesis.memory.linker.memory_links") as mock_crud:
        mock_crud.create = AsyncMock(return_value=("src", "tgt"))
        links = await linker.auto_link("mem-1", [0.1] * 1024)

    assert links[0].link_type == "extends"
    assert links[1].link_type == "supports"


@pytest.mark.asyncio()
async def test_auto_link_respects_max_links(linker):
    hits = [_hit(f"other-{i}", 0.85) for i in range(10)]
    with patch("genesis.memory.linker.search", return_value=hits), \
         patch("genesis.memory.linker.memory_links") as mock_crud:
        mock_crud.create = AsyncMock(return_value=("src", "tgt"))
        links = await linker.auto_link("mem-1", [0.1] * 1024, max_links=3)

    assert len(links) == 3


@pytest.mark.asyncio()
async def test_count_links_delegates(linker):
    with patch("genesis.memory.linker.memory_links") as mock_crud:
        mock_crud.count_links = AsyncMock(return_value=7)
        result = await linker.count_links("mem-1")

    assert result == 7


@pytest.mark.asyncio()
async def test_auto_link_empty_results(linker):
    with patch("genesis.memory.linker.search", return_value=[]), \
         patch("genesis.memory.linker.memory_links") as mock_crud:
        mock_crud.create = AsyncMock(return_value=("src", "tgt"))
        links = await linker.auto_link("mem-1", [0.1] * 1024)

    assert links == []
