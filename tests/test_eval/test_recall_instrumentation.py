"""Tests for memory recall quality instrumentation.

Covers: recall_diagnostics emitter, recall_used emitter,
emit_recall_fired return value, and fire-and-forget safety.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from genesis.db.crud import j9_eval
from genesis.eval.j9_hooks import (
    emit_recall_diagnostics,
    emit_recall_fired,
    emit_recall_used,
)

# ── emit_recall_fired returns event_id ──────────────────────────────────────


async def test_emit_recall_fired_returns_event_id(db):
    event_id = await emit_recall_fired(
        db,
        query="test query",
        result_count=5,
        top_scores=[0.8, 0.7, 0.6],
        memory_ids=["m1", "m2"],
        latency_ms=42.0,
        source="both",
    )
    assert event_id is not None
    assert isinstance(event_id, str)
    assert len(event_id) > 0


async def test_emit_recall_fired_returns_none_on_error():
    """Fire-and-forget: returns None on failure, does not raise."""
    bad_db = AsyncMock()
    bad_db.execute = AsyncMock(side_effect=RuntimeError("db gone"))

    result = await emit_recall_fired(
        bad_db,
        query="test",
        result_count=0,
        top_scores=[],
        memory_ids=[],
        latency_ms=0,
        source="both",
    )
    assert result is None


# ── update_recall_metrics (MEM-003: enrich the single event in place) ───────


async def test_update_recall_metrics_merges(db):
    from genesis.eval.j9_hooks import update_recall_metrics

    eid = await emit_recall_fired(
        db, query="q", result_count=1, top_scores=[], memory_ids=[],
        latency_ms=1.0, source="both",
    )
    await update_recall_metrics(
        db, eid, mode="auto", pipeline_used="standard", result_count=5,
    )
    events = await j9_eval.get_events(db, event_type="recall_fired")
    assert len(events) == 1  # enriched in place, not duplicated
    assert events[0]["metrics"]["mode"] == "auto"
    assert events[0]["metrics"]["pipeline_used"] == "standard"
    assert events[0]["metrics"]["result_count"] == 5


async def test_update_recall_metrics_fire_and_forget():
    """Should never propagate errors."""
    from genesis.eval.j9_hooks import update_recall_metrics

    bad_db = AsyncMock()
    bad_db.execute = AsyncMock(side_effect=RuntimeError("db gone"))
    # Should not raise
    await update_recall_metrics(bad_db, "evt-x", mode="auto")


# ── emit_recall_diagnostics ─────────────────────────────────────────────────


async def test_emit_recall_diagnostics_stores_event(db):
    await emit_recall_diagnostics(
        db,
        recall_event_id="evt-123",
        qdrant_pool_size=42,
        fts_pool_size=35,
        event_pool_size=0,
        total_candidates=60,
        overlap_count=17,
        score_spread=0.0234,
        embedding_available=True,
        intent_category="WHAT",
        intent_confidence=0.85,
        query_expanded=True,
    )

    events = await j9_eval.get_events(db, event_type="recall_diagnostics")
    assert len(events) == 1

    m = events[0]["metrics"]
    assert m["qdrant_pool"] == 42
    assert m["fts_pool"] == 35
    assert m["event_pool"] == 0
    assert m["total_candidates"] == 60
    assert m["overlap"] == 17
    assert m["score_spread"] == 0.0234
    assert m["embedding_available"] is True
    assert m["intent"] == "WHAT"
    assert m["intent_confidence"] == 0.85
    assert m["query_expanded"] is True
    assert events[0]["subject_id"] == "evt-123"


async def test_emit_recall_diagnostics_fire_and_forget():
    """Should not propagate errors."""
    bad_db = AsyncMock()
    bad_db.execute = AsyncMock(side_effect=RuntimeError("db gone"))

    # Should not raise
    await emit_recall_diagnostics(
        bad_db,
        recall_event_id=None,
        qdrant_pool_size=0,
        fts_pool_size=0,
        event_pool_size=0,
        total_candidates=0,
        overlap_count=0,
        score_spread=None,
        embedding_available=False,
        intent_category="GENERAL",
        intent_confidence=0.5,
        query_expanded=False,
    )


async def test_emit_recall_diagnostics_null_score_spread(db):
    """score_spread is None when no results."""
    await emit_recall_diagnostics(
        db,
        recall_event_id=None,
        qdrant_pool_size=0,
        fts_pool_size=0,
        event_pool_size=0,
        total_candidates=0,
        overlap_count=0,
        score_spread=None,
        embedding_available=False,
        intent_category="GENERAL",
        intent_confidence=0.5,
        query_expanded=False,
    )

    events = await j9_eval.get_events(db, event_type="recall_diagnostics")
    assert len(events) == 1
    assert events[0]["metrics"]["score_spread"] is None


# ── emit_recall_used ────────────────────────────────────────────────────────


async def test_emit_recall_used_stores_event(db):
    await emit_recall_used(
        db,
        memory_ids=["mem-1", "mem-2", "mem-3"],
        source="memory_expand",
    )

    events = await j9_eval.get_events(db, event_type="recall_used")
    assert len(events) == 1

    m = events[0]["metrics"]
    assert m["memory_ids"] == ["mem-1", "mem-2", "mem-3"]
    assert m["source"] == "memory_expand"
    assert m["used"] is True
    assert m["count"] == 3


async def test_emit_recall_used_truncates_long_list(db):
    """Memory IDs are capped at 20."""
    ids = [f"mem-{i}" for i in range(30)]
    await emit_recall_used(db, memory_ids=ids)

    events = await j9_eval.get_events(db, event_type="recall_used")
    assert len(events[0]["metrics"]["memory_ids"]) == 20


async def test_emit_recall_used_fire_and_forget():
    """Should not propagate errors."""
    bad_db = AsyncMock()
    bad_db.execute = AsyncMock(side_effect=RuntimeError("db gone"))

    # Should not raise
    await emit_recall_used(bad_db, memory_ids=["mem-1"])
