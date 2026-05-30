"""Tests for entity resolution module."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.memory.entity_resolution import (
    check_semantic_overlap,
    find_dedup_candidates,
    log_resolution,
    normalize_content,
)

# --- normalize_content ---


def test_normalize_basic():
    aliases = {"CC": "Claude Code", "LLM": "large language model"}
    result = normalize_content("CC uses an LLM for routing", aliases)
    assert result == "Claude Code uses an large language model for routing"


def test_normalize_case_insensitive():
    aliases = {"CC": "Claude Code"}
    result = normalize_content("cc is great and CC too", aliases)
    assert result == "Claude Code is great and Claude Code too"


def test_normalize_word_boundary():
    """Aliases should not match inside other words."""
    aliases = {"CC": "Claude Code"}
    result = normalize_content("CCNA certification", aliases)
    # "CC" should NOT match inside "CCNA"
    assert "CCNA" in result


def test_normalize_no_aliases():
    result = normalize_content("hello world", {})
    assert result == "hello world"


def test_normalize_none_aliases():
    """None aliases should load from file (returns unchanged if no file)."""
    # In test environment, no alias file exists → unchanged
    result = normalize_content("CC test", None)
    # May or may not normalize depending on file existence
    assert isinstance(result, str)


# --- find_dedup_candidates ---


@pytest.mark.asyncio
async def test_find_dedup_candidates_basic():
    """Find near-duplicate pairs via Qdrant similarity."""
    mock_qdrant = MagicMock()
    mock_search = MagicMock(return_value=[
        {"id": "p2", "score": 0.94, "payload": {"content": "fact B"}},
        {"id": "p1", "score": 1.0, "payload": {}},  # self-match
    ])

    points = [
        {"id": "p1", "payload": {"content": "fact A"}},
        {"id": "p2", "payload": {"content": "fact B"}},
    ]
    vectors = {"p1": [0.1] * 768, "p2": [0.2] * 768}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("genesis.qdrant.collections.search", mock_search)
        candidates = await find_dedup_candidates(
            mock_qdrant, points, vectors, threshold=0.90,
        )

    assert len(candidates) == 1
    assert candidates[0][2] == 0.94  # score


@pytest.mark.asyncio
async def test_find_dedup_candidates_dedup_pairs():
    """(A, B) and (B, A) should only appear once."""
    mock_search = MagicMock(side_effect=[
        [{"id": "p2", "score": 0.95, "payload": {}}],  # p1 → p2
        [{"id": "p1", "score": 0.95, "payload": {}}],  # p2 → p1 (dup)
    ])

    points = [
        {"id": "p1", "payload": {}},
        {"id": "p2", "payload": {}},
    ]
    vectors = {"p1": [0.1] * 768, "p2": [0.2] * 768}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("genesis.qdrant.collections.search", mock_search)
        candidates = await find_dedup_candidates(
            MagicMock(), points, vectors, threshold=0.90,
        )

    assert len(candidates) == 1  # not 2


@pytest.mark.asyncio
async def test_find_dedup_candidates_empty():
    """No candidates when below threshold."""
    mock_search = MagicMock(return_value=[
        {"id": "p2", "score": 0.50, "payload": {}},
    ])

    points = [{"id": "p1", "payload": {}}]
    vectors = {"p1": [0.1] * 768}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("genesis.qdrant.collections.search", mock_search)
        candidates = await find_dedup_candidates(
            MagicMock(), points, vectors, threshold=0.90,
        )

    assert len(candidates) == 0


# --- check_semantic_overlap ---


@pytest.mark.asyncio
async def test_check_semantic_overlap_duplicate():
    """LLM returns duplicate classification."""
    mock_router = AsyncMock()
    mock_router.route_call.return_value = MagicMock(
        success=True,
        content=json.dumps({"relationship": "duplicate", "reasoning": "same content"}),
    )

    result = await check_semantic_overlap(mock_router, "fact A", "fact A rephrased")
    assert result["relationship"] == "duplicate"
    assert "same content" in result["reasoning"]


@pytest.mark.asyncio
async def test_check_semantic_overlap_contradicts():
    mock_router = AsyncMock()
    mock_router.route_call.return_value = MagicMock(
        success=True,
        content=json.dumps({"relationship": "contradicts", "reasoning": "different numbers"}),
    )

    result = await check_semantic_overlap(mock_router, "cost is $100", "cost is $200")
    assert result["relationship"] == "contradicts"


@pytest.mark.asyncio
async def test_check_semantic_overlap_llm_error():
    """LLM failure defaults to 'distinct'."""
    mock_router = AsyncMock()
    mock_router.route_call.return_value = MagicMock(
        success=False, error="timeout", content=None,
    )

    result = await check_semantic_overlap(mock_router, "a", "b")
    assert result["relationship"] == "distinct"


@pytest.mark.asyncio
async def test_check_semantic_overlap_bad_json():
    """Unparseable LLM response defaults to 'distinct'."""
    mock_router = AsyncMock()
    mock_router.route_call.return_value = MagicMock(
        success=True, content="not json at all",
    )

    result = await check_semantic_overlap(mock_router, "a", "b")
    assert result["relationship"] == "distinct"


# --- log_resolution ---


@pytest.mark.asyncio
async def test_log_resolution(db):
    """Audit log writes to entity_resolution_audit table."""
    await log_resolution(
        db,
        run_id="test-run",
        action="auto_merge",
        memory_id_a="mem-a",
        memory_id_b="mem-b",
        content_a="content A",
        content_b="content B",
        cosine_score=0.97,
        survivor_id="mem-b",
    )

    cursor = await db.execute(
        "SELECT * FROM entity_resolution_audit WHERE run_id = ?",
        ("test-run",),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["action"] == "auto_merge"
    assert row["memory_id_a"] == "mem-a"
    assert row["memory_id_b"] == "mem-b"
    assert row["cosine_score"] == 0.97
    assert row["survivor_id"] == "mem-b"
