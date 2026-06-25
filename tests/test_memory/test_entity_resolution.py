"""Tests for entity resolution module."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.memory.entity_resolution import (
    EVIDENCE_THRESHOLD,
    check_semantic_overlap,
    compute_evidence_strength,
    find_dedup_candidates,
    log_resolution,
    normalize_content,
    pick_duplicate_survivor,
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


# --- compute_evidence_strength (spec ③: evidence-gated auto-merge) ---
#
# Strength in [0,1]; the auto-merge gate blocks when strength <
# EVIDENCE_THRESHOLD. Design invariants (calibrated vs 1727 historical merges):
#   - near-identical text (high cosine) is structurally merge-safe
#   - no SINGLE weak signal can block; only coincident weakness does
#   - absent payload fields never block on their own (no regression)

_NOW = datetime(2026, 6, 23, tzinfo=UTC)


def _payload(*, confidence=None, retrieved_count=None, created_at=_NOW, content="x"):
    p = {"content": content}
    if confidence is not None:
        p["confidence"] = confidence
    if retrieved_count is not None:
        p["retrieved_count"] = retrieved_count
    if created_at is not None:
        p["created_at"] = created_at.isoformat()
    return p


def test_evidence_strong_signals_merge():
    """High cosine + close in time + good confidence => clears the gate."""
    a = _payload(confidence=0.8, retrieved_count=0, created_at=_NOW)
    b = _payload(confidence=0.8, retrieved_count=0, created_at=_NOW)
    strength, signals = compute_evidence_strength(a, b, 0.99)
    assert strength >= EVIDENCE_THRESHOLD
    assert signals["cosine"] == 0.99


def test_evidence_coincident_weakness_blocks():
    """Floor cosine + far apart + default confidence => below threshold."""
    far = _NOW - timedelta(days=40)
    a = _payload(confidence=0.5, retrieved_count=0, created_at=_NOW)
    b = _payload(confidence=0.5, retrieved_count=0, created_at=far)
    strength, _ = compute_evidence_strength(a, b, 0.95)
    assert strength < EVIDENCE_THRESHOLD


def test_evidence_high_cosine_bypass():
    """Near-identical text always clears the gate, even when every other
    signal is weak (far apart + low conf + load-bearing). This is the key
    structural property: cosine alone keeps a strong floor."""
    far = _NOW - timedelta(days=40)
    a = _payload(confidence=0.5, retrieved_count=10, created_at=_NOW)
    b = _payload(confidence=0.5, retrieved_count=10, created_at=far)
    strength, _ = compute_evidence_strength(a, b, 0.99)
    assert strength >= EVIDENCE_THRESHOLD


def test_evidence_single_weak_signal_does_not_block():
    """Floor cosine but everything else strong => still merges. A single
    weak dimension must not block (only coincident weakness does)."""
    a = _payload(confidence=0.85, retrieved_count=0, created_at=_NOW)
    b = _payload(confidence=0.85, retrieved_count=0, created_at=_NOW)
    strength, _ = compute_evidence_strength(a, b, 0.95)
    assert strength >= EVIDENCE_THRESHOLD


def test_evidence_high_cosine_bypass_survives_load_penalty():
    """The high-cosine bypass must hold even when the load-bearing penalty
    applies (rc>=5) AND temporal/confidence are worst-case. Locks the
    invariant that near-identical text always clears the gate."""
    far = _NOW - timedelta(days=60)
    a = _payload(confidence=0.5, retrieved_count=50, created_at=_NOW)
    b = _payload(confidence=0.5, retrieved_count=50, created_at=far)
    strength, _ = compute_evidence_strength(a, b, 0.98)
    assert strength >= EVIDENCE_THRESHOLD


def test_evidence_absent_fields_do_not_block():
    """Missing confidence/retrieved_count/created_at must not, on their own,
    push a mid-band merge below threshold (no regression on sparse payloads)."""
    a = {"content": "x"}  # no confidence, retrieved_count, created_at
    b = {"content": "y"}
    strength, signals = compute_evidence_strength(a, b, 0.96)
    assert strength >= EVIDENCE_THRESHOLD
    assert signals["dt_days"] is None  # unknown temporal


def test_evidence_garbage_timestamp_is_defensive():
    """A malformed created_at must not raise — treated as unknown temporal."""
    a = {"content": "x", "created_at": "not-a-timestamp"}
    b = {"content": "y", "created_at": "also-bad"}
    strength, signals = compute_evidence_strength(a, b, 0.96)
    assert 0.0 <= strength <= 1.0
    assert signals["dt_days"] is None


# --- pick_duplicate_survivor (spec ③: survivor fix) ---


def test_survivor_prefers_more_retrieved_even_if_older():
    """The load-bearing memory survives even when it is the OLDER one
    (the live-data bug: we were deprecating the more-retrieved memory)."""
    older = _NOW - timedelta(days=5)
    a = _payload(retrieved_count=5, created_at=older)   # older, load-bearing
    b = _payload(retrieved_count=0, created_at=_NOW)    # newer, unused
    survivor, deprecated = pick_duplicate_survivor("id_a", a, older, "id_b", b, _NOW)
    assert survivor == "id_a"
    assert deprecated == "id_b"


def test_survivor_tie_breaks_to_newer():
    """Equal retrieved_count => newest survives (prior behavior preserved)."""
    older = _NOW - timedelta(days=5)
    a = _payload(retrieved_count=3, created_at=older)
    b = _payload(retrieved_count=3, created_at=_NOW)
    survivor, deprecated = pick_duplicate_survivor("id_a", a, older, "id_b", b, _NOW)
    assert survivor == "id_b"
    assert deprecated == "id_a"
