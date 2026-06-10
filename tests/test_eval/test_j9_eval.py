"""Tests for J-9 eval infrastructure: CRUD, hooks, and aggregator math."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.db.crud import j9_eval
from genesis.eval.j9_aggregator import (
    _grade_awareness,
    _grade_ego,
    _grade_memory,
    _grade_procedural,
    _grade_reflection,
    _score_to_grade,
    _simple_slope,
)
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


# ── Subsystem grading ──────────────────────────────────────────────────────


def test_score_to_grade_boundaries():
    assert _score_to_grade(95) == "A"
    assert _score_to_grade(90) == "A"
    assert _score_to_grade(89.9) == "B"
    assert _score_to_grade(80) == "B"
    assert _score_to_grade(70) == "C"
    assert _score_to_grade(60) == "D"
    assert _score_to_grade(59.9) == "F"
    assert _score_to_grade(0) == "F"
    assert _score_to_grade(None) is None


async def test_grade_memory_with_full_data(db):
    """Memory grade computes from precision@5, MRR, and usage_rate."""
    dimension_results = {
        "memory": {
            "precision_at_5": 0.85,
            "mrr": 0.90,
            "usage_rate": 0.70,
            "total_recalls": 50,
        },
    }
    result = await _grade_memory(db, "2026-05-01", "2026-05-08", dimension_results)
    assert result["grade"] is not None
    assert result["score"] is not None
    assert result["sample_count"] == 50
    # Score should be weighted average: 0.85*0.4 + 0.90*0.3 + 0.70*0.3 = 0.82 → 82
    assert 80 <= result["score"] <= 85


async def test_grade_memory_insufficient_data(db):
    """Memory grade returns null when sample count too low."""
    dimension_results = {
        "memory": {
            "precision_at_5": None,
            "mrr": None,
            "usage_rate": None,
            "total_recalls": 0,
        },
    }
    result = await _grade_memory(db, "2026-05-01", "2026-05-08", dimension_results)
    assert result["grade"] is None
    assert result["score"] is None
    assert "insufficient" in result.get("reason", "")


async def test_grade_ego_with_data(db):
    """Ego grade computes from approval rate and execution success."""
    dimension_results = {
        "ego": {
            "approval_rate": 0.24,
            "execution_success_rate": 1.0,
            "confidence_calibration": {
                "0.6-0.8": {"count": 9, "success_rate": 0.44},
                "0.8-1.0": {"count": 16, "success_rate": 0.13},
            },
            "total_proposals": 29,
        },
    }
    result = await _grade_ego(db, "2026-05-01", "2026-05-08", dimension_results)
    assert result["grade"] is not None
    assert result["score"] is not None
    assert result["sample_count"] == 29
    # Calibration + execution dominate; approval demoted to 20%
    assert result["score"] < 70  # C or below


async def test_grade_procedural_insufficient_invocations(db):
    """Procedural grade requires minimum invocations."""
    dimension_results = {
        "procedure": {
            "success_rate": None,
            "mean_confidence": 0.65,
            "invocation_count": 2,
            "total_procedures": 30,
        },
    }
    result = await _grade_procedural(db, "2026-05-01", "2026-05-08", dimension_results)
    assert result["grade"] is None
    assert "insufficient" in result.get("reason", "")


async def test_grade_procedural_null_success_rate(db):
    """Procedural grade returns no-grade when success_rate is null (primary factor)."""
    dimension_results = {
        "procedure": {
            "success_rate": None,
            "mean_confidence": 0.8,
            "invocation_count": 20,
            "total_procedures": 30,
        },
    }
    result = await _grade_procedural(db, "2026-05-01", "2026-05-08", dimension_results)
    assert result["grade"] is None
    assert "success_rate" in result.get("reason", "")


async def test_zero_fill_single_null_penalizes(db):
    """A single null factor is treated as 0.0, not skipped."""
    from genesis.eval.j9_aggregator import _score_with_zero_fill

    # All present: (0.8 * 0.4 + 0.6 * 0.3 + 0.7 * 0.3) * 100 ≈ 71.0
    all_present = [(0.8, 0.4), (0.6, 0.3), (0.7, 0.3)]
    assert abs(_score_with_zero_fill(all_present, max_nulls=1) - 71.0) < 0.01

    # One null: (0.8 * 0.4 + 0.0 * 0.3 + 0.7 * 0.3) * 100 ≈ 53.0
    one_null = [(0.8, 0.4), (None, 0.3), (0.7, 0.3)]
    assert abs(_score_with_zero_fill(one_null, max_nulls=1) - 53.0) < 0.01


async def test_zero_fill_multiple_nulls_returns_none(db):
    """More than max_nulls null factors → no grade."""
    from genesis.eval.j9_aggregator import _score_with_zero_fill

    two_nulls = [(0.8, 0.4), (None, 0.3), (None, 0.3)]
    assert _score_with_zero_fill(two_nulls, max_nulls=1) is None


async def test_grade_awareness_from_ticks(db):
    """Awareness grade uses tick_regularity + signal_completeness."""
    import json as _json

    # Build realistic signals_json with 20 distinct signal names
    signals = [{"name": f"signal_{i}", "value": 0.5} for i in range(20)]
    signals_json = _json.dumps(signals)

    for i in range(25):
        depth = ["Micro", "Light", "Deep", "Strategic"][i % 4] if i < 20 else None
        await db.execute(
            "INSERT INTO awareness_ticks (id, created_at, source, classified_depth, "
            "signals_json, scores_json) VALUES (?, ?, 'scheduled', ?, ?, '[]')",
            (f"tick-{i}", "2026-05-05T12:00:00Z", depth, signals_json),
        )
    await db.commit()

    result = await _grade_awareness(db, "2026-05-01", "2026-05-08", {})
    assert result["grade"] is not None
    assert result["score"] is not None
    assert result["sample_count"] == 25
    assert result["factors"]["classified_count"] == 20
    # Signal completeness: 20 unique signals vs 18 expected → 1.0 (capped)
    assert result["factors"]["signal_completeness"] == 1.0
    assert result["factors"]["unique_signals"] == 20


async def test_grade_awareness_empty_signals(db):
    """Signal_completeness = 0.0 when signals_json is empty, not a crash."""
    for i in range(25):
        await db.execute(
            "INSERT INTO awareness_ticks (id, created_at, source, classified_depth, "
            "signals_json, scores_json) VALUES (?, ?, 'scheduled', NULL, '[]', '[]')",
            (f"tick-{i}", "2026-05-05T12:00:00Z"),
        )
    await db.commit()
    result = await _grade_awareness(db, "2026-05-01", "2026-05-08", {})
    assert result["grade"] is not None
    assert result["factors"]["signal_completeness"] == 0.0
    assert result["factors"]["unique_signals"] == 0


async def test_grade_reflection_from_observations(db):
    """Reflection grade computes from observations data."""
    # Insert some observations
    for i in range(15):
        await db.execute(
            "INSERT INTO observations (id, type, source, content, priority, "
            "created_at, influenced_action) VALUES (?, ?, 'ego_cycle', 'test', "
            "'medium', ?, ?)",
            (f"obs-{i}", ["task_detected", "pattern", "insight"][i % 3],
             "2026-05-05T12:00:00Z", 1 if i < 10 else 0),
        )
    await db.commit()

    result = await _grade_reflection(db, "2026-05-01", "2026-05-08", {})
    assert result["grade"] is not None
    assert result["score"] is not None
    assert result["sample_count"] == 15
    assert result["factors"]["influence_rate"] == pytest.approx(10 / 15, abs=0.01)
    assert result["factors"]["type_count"] == 3


# ── CRUD: eval_subsystem_grades ────────────────────────────────────────────


async def test_insert_and_get_subsystem_grade(db):
    gid = await j9_eval.insert_subsystem_grade(
        db,
        period_start="2026-05-01",
        period_end="2026-05-08",
        period_type="weekly",
        subsystem="memory",
        grade="B",
        score=83.5,
        factors={"precision_at_5": 0.85, "mrr": 0.90},
        sample_count=50,
    )
    assert gid

    grades = await j9_eval.get_subsystem_grades(db, subsystem="memory")
    assert len(grades) == 1
    assert grades[0]["grade"] == "B"
    assert grades[0]["score"] == 83.5
    assert grades[0]["factors"]["precision_at_5"] == 0.85


async def test_get_latest_subsystem_grades(db):
    for sub in ["memory", "ego"]:
        await j9_eval.insert_subsystem_grade(
            db,
            period_start="2026-05-01",
            period_end="2026-05-08",
            period_type="weekly",
            subsystem=sub,
            grade="C",
            score=72.0,
            factors={},
            sample_count=10,
        )

    latest = await j9_eval.get_latest_subsystem_grades(db)
    assert len(latest) == 2
    subsystems = {g["subsystem"] for g in latest}
    assert subsystems == {"memory", "ego"}
