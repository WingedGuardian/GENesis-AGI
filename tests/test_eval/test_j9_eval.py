"""Tests for J-9 eval infrastructure: CRUD, hooks, and aggregator math."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.db.crud import j9_eval
from genesis.eval.j9_aggregator import _simple_slope
from genesis.eval.j9_hooks import (
    emit_procedure_invoked,
    emit_procedure_outcome,
    emit_proposal_resolved,
    emit_recall_fired,
)

# ── CRUD: eval_events ────────────────────────────────────────────────────────


async def test_insert_and_get_event(db):
    eid = await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_fired",
        metrics={"query": "test query", "result_count": 3},
        session_id="sess-1",
    )
    assert eid
    events = await j9_eval.get_events(db, dimension="memory")
    assert len(events) == 1
    assert events[0]["event_type"] == "recall_fired"
    assert events[0]["metrics"]["query"] == "test query"
    assert events[0]["session_id"] == "sess-1"


async def test_get_events_filter_by_type(db):
    await j9_eval.insert_event(db, dimension="memory", event_type="recall_fired", metrics={})
    await j9_eval.insert_event(db, dimension="memory", event_type="recall_relevance", metrics={})
    await j9_eval.insert_event(db, dimension="ego", event_type="proposal_resolved", metrics={})

    fired = await j9_eval.get_events(db, event_type="recall_fired")
    assert len(fired) == 1

    memory = await j9_eval.get_events(db, dimension="memory")
    assert len(memory) == 2

    ego = await j9_eval.get_events(db, dimension="ego")
    assert len(ego) == 1


async def test_count_events(db):
    await j9_eval.insert_event(db, dimension="memory", event_type="recall_fired", metrics={})
    await j9_eval.insert_event(db, dimension="memory", event_type="recall_fired", metrics={})
    await j9_eval.insert_event(db, dimension="ego", event_type="proposal_resolved", metrics={})

    assert await j9_eval.count_events(db, dimension="memory") == 2
    assert await j9_eval.count_events(db, dimension="ego") == 1
    assert await j9_eval.count_events(db) == 3


async def test_get_events_with_session_filter(db):
    await j9_eval.insert_event(
        db, dimension="memory", event_type="recall_fired",
        metrics={}, session_id="sess-A",
    )
    await j9_eval.insert_event(
        db, dimension="memory", event_type="recall_fired",
        metrics={}, session_id="sess-B",
    )
    results = await j9_eval.get_events(db, session_id="sess-A")
    assert len(results) == 1
    assert results[0]["session_id"] == "sess-A"


# ── CRUD: eval_snapshots ─────────────────────────────────────────────────────


async def test_insert_and_get_snapshot(db):
    sid = await j9_eval.insert_snapshot(
        db,
        period_start="2026-05-01T00:00:00Z",
        period_end="2026-05-08T00:00:00Z",
        period_type="weekly",
        dimension="memory",
        metrics={"precision_at_5": 0.72, "hit_rate": 0.85},
        sample_count=42,
    )
    assert sid

    snapshots = await j9_eval.get_snapshots(db, dimension="memory")
    assert len(snapshots) == 1
    assert snapshots[0]["metrics"]["precision_at_5"] == 0.72
    assert snapshots[0]["sample_count"] == 42


async def test_get_latest_snapshot(db):
    await j9_eval.insert_snapshot(
        db,
        period_start="2026-04-24T00:00:00Z",
        period_end="2026-05-01T00:00:00Z",
        period_type="weekly",
        dimension="ego",
        metrics={"approval_rate": 0.30},
        sample_count=10,
    )
    await j9_eval.insert_snapshot(
        db,
        period_start="2026-05-01T00:00:00Z",
        period_end="2026-05-08T00:00:00Z",
        period_type="weekly",
        dimension="ego",
        metrics={"approval_rate": 0.45},
        sample_count=15,
    )

    latest = await j9_eval.get_latest_snapshot(db, dimension="ego")
    assert latest is not None
    assert latest["metrics"]["approval_rate"] == 0.45


async def test_get_latest_snapshot_returns_none(db):
    result = await j9_eval.get_latest_snapshot(db, dimension="memory")
    assert result is None


async def test_snapshot_composite_dimension_accepted(db):
    """The 'composite' dimension must be valid for eval_snapshots."""
    sid = await j9_eval.insert_snapshot(
        db,
        period_start="2026-05-01T00:00:00Z",
        period_end="2026-05-08T00:00:00Z",
        period_type="weekly",
        dimension="composite",
        metrics={"go_criteria_met": 2},
        sample_count=5,
    )
    assert sid
    snap = await j9_eval.get_latest_snapshot(db, dimension="composite")
    assert snap is not None
    assert snap["metrics"]["go_criteria_met"] == 2


# ── Hooks: fire-and-forget safety ────────────────────────────────────────────


async def test_emit_recall_fired_writes_event(db):
    await emit_recall_fired(
        db,
        query="what is the user's timezone?",
        result_count=3,
        top_scores=[0.8, 0.6, 0.4],
        memory_ids=["mem1", "mem2", "mem3"],
        latency_ms=450.5,
        source="both",
        session_id="sess-test",
    )
    events = await j9_eval.get_events(db, dimension="memory", event_type="recall_fired")
    assert len(events) == 1
    m = events[0]["metrics"]
    assert m["query"] == "what is the user's timezone?"
    assert m["result_count"] == 3
    assert m["latency_ms"] == 450.5


async def test_emit_recall_fired_survives_db_error():
    """Hooks must never raise — even if the DB is broken."""
    bad_db = AsyncMock(spec=["execute", "commit"])
    bad_db.execute.side_effect = Exception("DB is on fire")

    # Should not raise
    await emit_recall_fired(
        bad_db,
        query="test",
        result_count=0,
        top_scores=[],
        memory_ids=[],
        latency_ms=0,
        source="both",
    )


async def test_emit_proposal_resolved_writes_event(db):
    await emit_proposal_resolved(
        db,
        proposal_id="prop-123",
        status="approved",
        confidence=0.75,
        action_type="investigate",
    )
    events = await j9_eval.get_events(db, dimension="ego")
    assert len(events) == 1
    assert events[0]["metrics"]["status"] == "approved"
    assert events[0]["metrics"]["confidence"] == 0.75


async def test_emit_proposal_resolved_survives_db_error():
    bad_db = AsyncMock(spec=["execute", "commit"])
    bad_db.execute.side_effect = Exception("kaboom")
    await emit_proposal_resolved(
        bad_db, proposal_id="x", status="approved",
    )


async def test_emit_procedure_invoked_writes_event(db):
    await emit_procedure_invoked(
        db,
        procedure_id="proc-abc",
        confidence=0.85,
        matched_tags=["git", "worktree"],
        session_id="sess-42",
    )
    events = await j9_eval.get_events(db, dimension="procedure", event_type="procedure_invoked")
    assert len(events) == 1
    assert events[0]["metrics"]["confidence_at_invoke"] == 0.85


async def test_emit_procedure_outcome_writes_event(db):
    await emit_procedure_outcome(
        db,
        procedure_id="proc-abc",
        success=True,
        confidence_after=0.9,
    )
    events = await j9_eval.get_events(db, dimension="procedure", event_type="procedure_outcome")
    assert len(events) == 1
    assert events[0]["metrics"]["success"] is True


# ── Aggregator math ──────────────────────────────────────────────────────────


def test_simple_slope_positive():
    assert _simple_slope([1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_simple_slope_negative():
    assert _simple_slope([3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_simple_slope_flat():
    assert _simple_slope([5.0, 5.0, 5.0]) == pytest.approx(0.0)


def test_simple_slope_two_points():
    assert _simple_slope([0.0, 1.0]) == pytest.approx(1.0)


def test_simple_slope_single_point():
    assert _simple_slope([42.0]) is None


def test_simple_slope_empty():
    assert _simple_slope([]) is None


def test_simple_slope_noisy():
    # Linear trend with noise: y ≈ 0.1x + 0.5
    values = [0.5, 0.62, 0.68, 0.82, 0.91]
    slope = _simple_slope(values)
    assert slope is not None
    assert slope > 0  # positive trend
    assert 0.08 < slope < 0.12  # roughly 0.1 per step
