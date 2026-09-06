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


async def test_update_event_metrics_merges_in_place(db):
    """update_event_metrics enriches an existing event's metrics without
    creating a second row (audit MEM-003: one recall_fired per logical recall)."""
    eid = await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_fired",
        metrics={"query": "q", "result_count": 1},
    )
    updated = await j9_eval.update_event_metrics(
        db,
        eid,
        result_count=5,
        mode="auto",
        pipeline_used="standard",
    )
    assert updated is True

    events = await j9_eval.get_events(db, event_type="recall_fired")
    assert len(events) == 1  # merged in place, NOT a second event
    m = events[0]["metrics"]
    assert m["query"] == "q"  # pre-existing field preserved
    assert m["result_count"] == 5  # overwritten
    assert m["mode"] == "auto"  # new field added
    assert m["pipeline_used"] == "standard"


async def test_update_event_metrics_missing_id_is_noop(db):
    """Updating a non-existent event returns False and inserts nothing."""
    updated = await j9_eval.update_event_metrics(db, "does-not-exist", foo=1)
    assert updated is False
    events = await j9_eval.get_events(db, dimension="memory")
    assert len(events) == 0


async def test_recall_entrenchment_aggregates(db):
    """MEM-005: the aggregator averages the per-recall entrenchment signal from
    recall_fired events, ignoring events that carry no entrenchment fields."""
    from genesis.eval.j9_aggregator import _recall_entrenchment

    for corr, rc, age in [(0.8, 5.0, 10.0), (0.6, 3.0, 20.0)]:
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_fired",
            metrics={"entrenchment_corr": corr, "mean_retrieved_count": rc, "mean_age_days": age},
        )
    # An older-style event without entrenchment fields must not skew the means.
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_fired",
        metrics={"query": "q"},
    )

    result = await _recall_entrenchment(db, since="2000-01-01", until="2099-01-01")
    assert result["entrenchment_corr_mean"] == pytest.approx(0.7)
    assert result["entrenchment_mean_retrieved_count"] == pytest.approx(4.0)
    assert result["entrenchment_mean_age_days"] == pytest.approx(15.0)
    assert result["entrenchment_sample"] == 2


async def test_memory_quality_includes_entrenchment_when_no_relevance(db):
    """Entrenchment surfaces even when there are no recall_relevance events
    (the early-return path still carries the MEM-005 keys)."""
    from genesis.eval.j9_aggregator import _compute_memory_quality

    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_fired",
        metrics={"entrenchment_corr": 0.5, "mean_retrieved_count": 2.0},
    )
    metrics, sample = await _compute_memory_quality(
        db,
        since="2000-01-01",
        until="2099-01-01",
    )
    assert metrics["precision_at_5"] is None  # no relevance events
    assert metrics["entrenchment_corr_mean"] == pytest.approx(0.5)
    assert metrics["entrenchment_sample"] == 1


# ── WS2-0: pool-size counts stamped onto the weekly memory snapshot ───────────


async def _seed_memory_row(db, memory_id, collection, embedding_status="embedded"):
    await db.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, collection, embedding_status) VALUES (?, ?, ?, ?)",
        (memory_id, "2026-07-17T00:00:00Z", collection, embedding_status),
    )
    await db.commit()


async def test_pool_size_counts_splits_episodic_by_collection(db):
    """Episodic is counted by collection — a knowledge_base row in
    memory_metadata must NOT inflate the episodic pool — and embedded is a
    distinct sub-count. Delta-based to tolerate any seed rows."""
    from genesis.db.crud import memory as memory_crud

    base = await memory_crud.pool_size_counts(db)
    await _seed_memory_row(db, "ep-1", "episodic_memory", "embedded")
    await _seed_memory_row(db, "ep-2", "episodic_memory", "fts5_only")
    await _seed_memory_row(db, "kb-1", "knowledge_base", "embedded")

    counts = await memory_crud.pool_size_counts(db)
    assert counts["pool_episodic_total"] - base["pool_episodic_total"] == 2
    assert counts["pool_episodic_embedded"] - base["pool_episodic_embedded"] == 1


async def test_pool_size_counts_retrievable_excludes_deprecated_and_expired(db):
    """pool_episodic_retrievable applies the SAME filters as the recall path:
    deprecated rows and bitemporally-expired rows count toward the total but
    NOT the retrievable pool — the denominator the drift hypothesis cares about."""
    from genesis.db.crud import memory as memory_crud

    base = await memory_crud.pool_size_counts(db)
    await _seed_memory_row(db, "live-1", "episodic_memory")  # retrievable
    await db.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, collection, embedding_status, deprecated) "
        "VALUES ('dep-1', '2026-07-17T00:00:00Z', 'episodic_memory', 'embedded', 1)",
    )
    await db.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, collection, embedding_status, invalid_at) "
        "VALUES ('exp-1', '2026-07-17T00:00:00Z', 'episodic_memory', 'embedded', "
        "'2000-01-01T00:00:00Z')",
    )
    await db.commit()

    counts = await memory_crud.pool_size_counts(db)
    # all three count toward the total; only the live one is retrievable
    assert counts["pool_episodic_total"] - base["pool_episodic_total"] == 3
    assert counts["pool_episodic_retrievable"] - base["pool_episodic_retrievable"] == 1


async def test_episodic_count_created_before(db):
    """The report's pool-reconstruction CRUD helper counts episodic rows created
    on or before a cutoff."""
    from genesis.db.crud import memory as memory_crud

    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES "
        "('old-1', '2026-01-01T00:00:00Z', 'episodic_memory'), "
        "('new-1', '2026-12-01T00:00:00Z', 'episodic_memory')",
    )
    await db.commit()
    before_mid = await memory_crud.episodic_count_created_before(db, "2026-06-15T00:00:00Z")
    before_future = await memory_crud.episodic_count_created_before(db, "2027-01-01T00:00:00Z")
    # new-1 (Dec) is excluded before mid-year, included before the future cutoff
    assert before_future - before_mid == 1


async def test_pool_size_counts_knowledge_units_and_links(db):
    from genesis.db.crud import memory as memory_crud

    base = await memory_crud.pool_size_counts(db)
    await db.execute(
        "INSERT INTO knowledge_units "
        "(id, project_type, domain, source_doc, concept, body, ingested_at) "
        "VALUES ('ku-1','p','d','s','c','b','2026-07-17T00:00:00Z')",
    )
    await db.execute(
        "INSERT INTO memory_links "
        "(source_id, target_id, link_type, strength, created_at) "
        "VALUES ('a','b','related_to',0.5,'2026-07-17T00:00:00Z')",
    )
    await db.commit()

    counts = await memory_crud.pool_size_counts(db)
    assert counts["pool_knowledge_units_total"] - base["pool_knowledge_units_total"] == 1
    assert counts["pool_memory_links_total"] - base["pool_memory_links_total"] == 1


async def test_pool_count_keys_match_output(db):
    """POOL_COUNT_KEYS must be the exact key set pool_size_counts emits — the
    aggregator's None-fallback keys are derived from it and must not drift."""
    from genesis.db.crud import memory as memory_crud

    counts = await memory_crud.pool_size_counts(db)
    assert set(counts) == set(memory_crud.POOL_COUNT_KEYS)


async def test_memory_quality_includes_pool_counts_on_early_return(db):
    """The no-relevance early-return path still carries the pool_* keys, so the
    trend series shape is stable from the first (empty) week onward."""
    from genesis.eval.j9_aggregator import _compute_memory_quality

    await _seed_memory_row(db, "ep-1", "episodic_memory")
    metrics, _ = await _compute_memory_quality(
        db,
        since="2000-01-01",
        until="2099-01-01",
    )
    assert metrics["precision_at_5"] is None
    from genesis.db.crud import memory as memory_crud

    for key in memory_crud.POOL_COUNT_KEYS:
        assert key in metrics
    assert metrics["pool_episodic_total"] >= 1


async def test_memory_quality_pool_failure_degrades_to_none(db, monkeypatch):
    """A pool-count failure degrades to None-valued pool keys WITHOUT losing the
    quality metrics — the pool read is strictly additive."""
    from genesis.db.crud import memory as memory_crud
    from genesis.eval import j9_aggregator

    async def _boom(_db):
        raise RuntimeError("pool count blew up")

    monkeypatch.setattr(memory_crud, "pool_size_counts", _boom)
    metrics, _ = await j9_aggregator._compute_memory_quality(
        db,
        since="2000-01-01",
        until="2099-01-01",
    )
    for key in memory_crud.POOL_COUNT_KEYS:
        assert metrics[key] is None
    assert "precision_at_5" in metrics  # quality series intact


# ── WS2-0: retrieval-efficacy readers (j9_eval_status trend + memory_stats) ───


def test_retrieval_trend_chronological_and_none_tolerant():
    """Newest-first snapshots become an oldest-first trend; a pre-WS2-0 snapshot
    (no pool_* keys) yields explicit None, never a KeyError or fabricated value."""
    from genesis.mcp.health.j9_eval import _retrieval_trend

    snaps = [  # DESC (newest-first), as get_snapshots returns
        {
            "period_end": "2026-07-17",
            "metrics": {"precision_at_5": 0.8, "hit_rate": 0.7, "pool_episodic_total": 55000},
        },
        {"period_end": "2026-07-10", "metrics": {"precision_at_5": 0.6}},  # pre-WS2-0
    ]
    trend = _retrieval_trend(snaps)
    assert [r["period_end"] for r in trend] == ["2026-07-10", "2026-07-17"]
    assert trend[0]["pool_episodic_total"] is None  # older snapshot → honest gap
    assert trend[1]["pool_episodic_total"] == 55000
    assert trend[1]["hit_rate"] == 0.7


def test_retrieval_trend_empty():
    from genesis.mcp.health.j9_eval import _retrieval_trend

    assert _retrieval_trend([]) == []


async def test_memory_quality_block_joins_snapshot_and_grade(db):
    """_memory_quality_block returns the latest memory snapshot headline joined
    to the memory subsystem grade — capacity and quality on one surface."""
    from genesis.mcp.memory.core import _memory_quality_block

    await j9_eval.insert_snapshot(
        db,
        period_start="2026-07-10",
        period_end="2026-07-17",
        period_type="weekly",
        dimension="memory",
        metrics={
            "precision_at_5": 0.82,
            "hit_rate": 0.75,
            "mrr": 0.6,
            "total_recalls": 40,
            "pool_episodic_total": 55000,
            "pool_knowledge_units_total": 3900,
        },
        sample_count=40,
    )
    await j9_eval.insert_subsystem_grade(
        db,
        period_start="2026-07-10",
        period_end="2026-07-17",
        period_type="weekly",
        subsystem="memory",
        grade="B",
        score=0.82,
        factors={},
        sample_count=40,
    )
    block = await _memory_quality_block(db)
    assert block["precision_at_5"] == 0.82
    assert block["pool_episodic_total"] == 55000
    assert block["grade"] == "B"
    assert block["grade_score"] == 0.82


async def test_memory_quality_block_grade_missing_is_none(db):
    """A snapshot with no grade row still returns the headline; grade is None."""
    from genesis.mcp.memory.core import _memory_quality_block

    await j9_eval.insert_snapshot(
        db,
        period_start="2026-07-10",
        period_end="2026-07-17",
        period_type="weekly",
        dimension="memory",
        metrics={"precision_at_5": 0.5},
        sample_count=1,
    )
    block = await _memory_quality_block(db)
    assert block["precision_at_5"] == 0.5
    assert block["grade"] is None


async def test_memory_quality_block_none_when_no_snapshot(db):
    from genesis.mcp.memory.core import _memory_quality_block

    assert await _memory_quality_block(db) is None


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
        db,
        dimension="memory",
        event_type="recall_fired",
        metrics={},
        session_id="sess-A",
    )
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_fired",
        metrics={},
        session_id="sess-B",
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
        bad_db,
        proposal_id="x",
        status="approved",
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
            # 25 of the 29 were actually judged (9 + 16 in the calibration
            # buckets); the rest never got an answer. The grade is gated on
            # THIS number, not on total_proposals — see _grade_ego.
            "total_resolved": 25,
        },
    }
    result = await _grade_ego(db, "2026-05-01", "2026-05-08", dimension_results)
    assert result["grade"] is not None
    assert result["score"] is not None
    assert result["sample_count"] == 25
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


async def test_grade_awareness_from_ticks(db):
    """Awareness grade uses tick_regularity + signal_completeness."""
    import json as _json

    from genesis.awareness.types import STEADY_STATE_SIGNALS

    # All steady-state signals present (value 0.0 when idle) → completeness 1.0.
    signals = [{"name": n, "value": 0.0} for n in sorted(STEADY_STATE_SIGNALS)]
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
    # All steady-state collectors producing output → completeness 1.0.
    assert result["factors"]["signal_completeness"] == 1.0
    assert result["factors"]["unique_signals"] == len(STEADY_STATE_SIGNALS)


async def test_grade_awareness_completeness_detects_dropped_collector(db):
    """A steady-state collector missing from ticks drops completeness below 1.0.

    Regression guard for the collector-drop bug: the old magic-denominator metric
    (distinct_names / 18) saturated at 1.0 and never detected a dropped collector.
    Scoring against the canonical STEADY_STATE_SIGNALS set makes a drop visible.
    """
    import json as _json

    from genesis.awareness.types import STEADY_STATE_SIGNALS

    # One steady-state signal absent from every tick (collector dropped).
    present = sorted(STEADY_STATE_SIGNALS - {"scheduled_job_health"})
    signals_json = _json.dumps([{"name": n, "value": 0.0} for n in present])

    for i in range(25):
        await db.execute(
            "INSERT INTO awareness_ticks (id, created_at, source, classified_depth, "
            "signals_json, scores_json) VALUES (?, ?, 'scheduled', NULL, ?, '[]')",
            (f"tick-{i}", "2026-05-05T12:00:00Z", signals_json),
        )
    await db.commit()

    result = await _grade_awareness(db, "2026-05-01", "2026-05-08", {})
    n = len(STEADY_STATE_SIGNALS)
    # factors["signal_completeness"] is round(_, 4) in the aggregator.
    assert result["factors"]["signal_completeness"] == round((n - 1) / n, 4)
    assert result["factors"]["signal_completeness"] < 1.0


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
            (
                f"obs-{i}",
                ["task_detected", "pattern", "insight"][i % 3],
                "2026-05-05T12:00:00Z",
                1 if i < 10 else 0,
            ),
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


# ── aggregator dedup (defends metrics against duplicate judgments) ────────────


def test_dedupe_by_pair_keeps_first():
    from genesis.eval.j9_aggregator import _dedupe_by_pair

    events = [
        {"metrics": {"recall_event_id": "r1", "memory_id": "m1", "relevance": 1.0}},
        {"metrics": {"recall_event_id": "r1", "memory_id": "m1", "relevance": 0.0}},
        {"metrics": {"recall_event_id": "r1", "memory_id": "m2", "relevance": 0.5}},
        {"metrics": {"foo": "bar"}},  # missing ids → kept as-is
    ]
    out = _dedupe_by_pair(events)
    assert len(out) == 3
    assert out[0]["metrics"]["relevance"] == 1.0  # first occurrence kept


async def test_memory_quality_ignores_duplicate_relevance(db):
    from genesis.eval.j9_aggregator import _compute_memory_quality

    # Duplicate (r1, m1) relevant judgments + one non-relevant (r1, m2).
    # Deduped precision for the recall = 1 relevant / 2 memories = 0.5
    # (without dedup it would be 2/3 = 0.667).
    for rel in (1.0, 1.0):
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_relevance",
            metrics={"recall_event_id": "r1", "memory_id": "m1", "relevance": rel},
        )
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_relevance",
        metrics={"recall_event_id": "r1", "memory_id": "m2", "relevance": 0.0},
    )

    metrics, total_recalls = await _compute_memory_quality(
        db,
        since="2000-01-01",
        until="2100-01-01",
    )
    assert total_recalls == 1
    assert metrics["precision_at_5"] == 0.5
    assert metrics["total_memories_judged"] == 2  # deduped, not 3


async def test_memory_quality_mrr_uses_stored_rank_not_insert_order(db):
    """MRR must use the retrieval rank persisted in the event metrics, NOT the
    DB/insert order. recall_relevance events are inserted CONCURRENTLY (arbitrary
    order, read back ORDER BY timestamp DESC), so list order != retrieval rank.

    Insert the only RELEVANT memory at rank 3 LAST (so it's returned FIRST by
    timestamp DESC); MRR must be 1/3 (first relevant is at rank 3), not 1/1.
    """
    from genesis.eval.j9_aggregator import _compute_memory_quality

    # Out of rank order: rank-1 (irrelevant) first … rank-3 (relevant) last.
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_relevance",
        metrics={"recall_event_id": "r1", "memory_id": "m1", "relevance": 0.0, "rank": 1},
    )
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_relevance",
        metrics={"recall_event_id": "r1", "memory_id": "m2", "relevance": 0.0, "rank": 2},
    )
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_relevance",
        metrics={"recall_event_id": "r1", "memory_id": "m3", "relevance": 1.0, "rank": 3},
    )

    metrics, _ = await _compute_memory_quality(db, since="2000-01-01", until="2100-01-01")
    # First relevant is at rank 3 → MRR ≈ 1/3 (aggregator rounds to 4dp).
    # The bug would give 1.0 (relevant returned first by timestamp DESC).
    assert metrics["mrr"] == pytest.approx(1.0 / 3.0, abs=1e-3)


async def test_insert_run_persists_metadata_json_per_result(db):
    """eval_results.metadata_json must be written (migration 0014's documented
    dual-write) — it mirrors scorer_detail so calibration/drift tooling can
    query the judge payload structurally without re-parsing scorer_detail.
    """
    import importlib
    import json as _json

    from genesis.eval import db as eval_db
    from genesis.eval.types import (
        EvalRunSummary,
        EvalTrigger,
        ScoredOutput,
        ScorerType,
        TaskCategory,
    )

    # eval_runs/eval_results come from migrations, not create_all_tables. Apply
    # the full eval_results column chain: 0002 (create) → 0003 (skipped) →
    # 0014 (metadata_json).
    for _mig in (
        "0002_add_eval_tables",
        "0003_eval_results_skipped",
        "0014_eval_results_metadata",
    ):
        await importlib.import_module(f"genesis.db.migrations.{_mig}").up(db)
    await db.commit()

    detail = _json.dumps({"judge_model": "x", "judge_score": 0.7, "rationale": "ok"})
    summary = EvalRunSummary(
        run_id="run_meta_1",
        model_id="m",
        model_profile="p",
        dataset="d",
        trigger=EvalTrigger.MANUAL,
        task_category=TaskCategory.CLASSIFICATION,
        total_cases=2,
        passed_cases=2,
        failed_cases=0,
        results=[
            ScoredOutput(
                case_id="c1",
                passed=True,
                score=0.7,
                actual_output="out",
                scorer_type=ScorerType.LLM_JUDGE,
                scorer_detail=detail,
            ),
            # Non-judge scorer: scorer_detail is a plain (non-JSON) string, so
            # metadata_json (the queryable JSON view) must be NULL, not that string.
            ScoredOutput(
                case_id="c2",
                passed=True,
                score=1.0,
                actual_output="3",
                scorer_type=ScorerType.EXACT_MATCH,
                scorer_detail="expected=3, got=3",
            ),
        ],
    )
    await eval_db.insert_run(db, summary)
    rows = {
        r[0]: r[1]
        for r in await (
            await db.execute(
                "SELECT case_id, metadata_json FROM eval_results WHERE run_id = 'run_meta_1'"
            )
        ).fetchall()
    }
    assert rows["c1"] == detail  # judge result → JSON dual-write
    assert rows["c2"] is None  # non-judge result → NULL (not the plain string)


# ── WS-1 A2: precision@3 + top-5 truncation ─────────────────────────────────


async def test_memory_quality_precision_at_3(db):
    """p@3 counts relevance in the top-3 by rank; p@5 stays precision-among-
    judged. Ranks 1-5 with relevance (1,0,1,0,1): p@5 = 3/5, p@3 = 2/3."""
    from genesis.eval.j9_aggregator import _compute_memory_quality

    for rank, rel in enumerate((1.0, 0.0, 1.0, 0.0, 1.0), 1):
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_relevance",
            metrics={
                "recall_event_id": "r1",
                "memory_id": f"m{rank}",
                "relevance": rel,
                "rank": rank,
            },
        )

    metrics, _ = await _compute_memory_quality(db, since="2000-01-01", until="2100-01-01")
    assert metrics["precision_at_5"] == 0.6
    assert metrics["precision_at_3"] == pytest.approx(2 / 3, abs=1e-3)
    assert metrics["precision_at_3_recalls"] == 1


async def test_memory_quality_precision_at_3_fewer_than_three(db):
    """With fewer than 3 judged memories the p@3 denominator is the judged
    count (min(3, judged)), not a hard 3."""
    from genesis.eval.j9_aggregator import _compute_memory_quality

    for rank, rel in ((1, 1.0), (2, 0.0)):
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_relevance",
            metrics={
                "recall_event_id": "r1",
                "memory_id": f"m{rank}",
                "relevance": rel,
                "rank": rank,
            },
        )

    metrics, _ = await _compute_memory_quality(db, since="2000-01-01", until="2100-01-01")
    assert metrics["precision_at_3"] == 0.5


async def test_memory_quality_truncates_to_top5(db):
    """A malformed emitter judging >5 memories per recall must not inflate the
    @5 denominator: rank-6 (the only relevant one) is cut, p@5 = 0/5."""
    from genesis.eval.j9_aggregator import _compute_memory_quality

    for rank in range(1, 6):
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_relevance",
            metrics={
                "recall_event_id": "r1",
                "memory_id": f"m{rank}",
                "relevance": 0.0,
                "rank": rank,
            },
        )
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_relevance",
        metrics={"recall_event_id": "r1", "memory_id": "m6", "relevance": 1.0, "rank": 6},
    )

    metrics, _ = await _compute_memory_quality(db, since="2000-01-01", until="2100-01-01")
    assert metrics["precision_at_5"] == 0.0
    assert metrics["hit_rate"] == 0.0  # the relevant memory was beyond top-5


async def test_precision_at_3_skips_unranked_recalls(db):
    """Pre-fix events without rank sort arbitrarily — an unranked recall is
    excluded from p@3 (None when no recall qualifies) while p@5 still computes."""
    from genesis.eval.j9_aggregator import _compute_memory_quality

    for i, rel in enumerate((1.0, 0.0, 1.0, 0.0), 1):
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_relevance",
            metrics={"recall_event_id": "r1", "memory_id": f"m{i}", "relevance": rel},
        )

    metrics, _ = await _compute_memory_quality(db, since="2000-01-01", until="2100-01-01")
    assert metrics["precision_at_5"] == 0.5
    assert metrics["precision_at_3"] is None
    assert metrics["precision_at_3_recalls"] == 0


async def test_memory_quality_reports_judge_prompt_versions(db):
    """The set of judge-prompt versions seen in-window is reported so a judge
    change reads as a series break; version-less events show as 'unversioned'."""
    from genesis.eval.j9_aggregator import _compute_memory_quality

    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_relevance",
        metrics={
            "recall_event_id": "r1",
            "memory_id": "m1",
            "relevance": 1.0,
            "rank": 1,
            "judge_prompt_version": "1",
        },
    )
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_relevance",
        metrics={"recall_event_id": "r2", "memory_id": "m2", "relevance": 0.0, "rank": 1},
    )

    metrics, _ = await _compute_memory_quality(db, since="2000-01-01", until="2100-01-01")
    assert metrics["judge_prompt_versions"] == ["1", "unversioned"]


# ── WS-1 A2: approvals dimension ─────────────────────────────────────────────


async def _approval(
    db, aid, *, action_type="other", status=None, resolved_at=None, resolved_by=None
):
    from genesis.db.crud import approval_requests as ar

    await ar.create(
        db,
        id=aid,
        action_type=action_type,
        action_class="reversible",
        description="d",
    )
    if status is not None:
        assert await ar.resolve(
            db,
            aid,
            status=status,
            resolved_at=resolved_at,
            resolved_by=resolved_by,
        )


async def test_compute_approvals_buckets(db):
    """Every bucket of the approvals dimension, incl. the M12 scaffold
    (human-resolved rejection → user_denied_count) and unknown surfacing."""
    from genesis.eval.j9_aggregator import _compute_approvals

    ts = "2026-06-05T12:00:00+00:00"
    # churn class: one human approve, one fail-closed system cancel
    await _approval(
        db,
        "a1",
        action_type="autonomous_cli_fallback",
        status="approved",
        resolved_at=ts,
        resolved_by="telegram:button:1",
    )
    await _approval(
        db,
        "a2",
        action_type="autonomous_cli_fallback",
        status="cancelled",
        resolved_at=ts,
        resolved_by="system",
    )
    # non-churn: explicit human denial (M12), timeout expiry, unknown resolver
    await _approval(db, "a3", status="rejected", resolved_at=ts, resolved_by="telegram:bare_text:1")
    await _approval(db, "a4", status="expired", resolved_at=ts, resolved_by="timeout_auto_expire")
    await _approval(db, "a5", status="approved", resolved_at=ts, resolved_by="manual_stale_cleanup")
    # still pending — resolved-population excludes it; pending_open gauge sees it
    await _approval(db, "a6")

    metrics, sample = await _compute_approvals(
        db,
        since="2000-01-01",
        until="2100-01-01",
    )
    assert sample == 5
    assert metrics["total_created"] == 6
    assert metrics["total_resolved"] == 5
    assert metrics["churn_total"] == 2
    assert metrics["churn_excluded_total"] == 3
    assert metrics["user_resolved"] == 2  # a1 + a3
    assert metrics["user_resolved_rate"] == 0.4
    assert metrics["user_resolved_rate_excl_churn"] == pytest.approx(1 / 3, abs=1e-3)
    assert metrics["auto_resolved"] == 2  # a2 + a4
    assert metrics["auto_expired"] == 1
    assert metrics["system_cancelled"] == 1
    assert metrics["rejection_count"] == 1
    assert metrics["user_denied_count"] == 1  # M12: human reject
    assert metrics["unknown_resolver_count"] == 1
    assert metrics["unknown_resolver_values"] == ["manual_stale_cleanup"]
    assert metrics["pending_open"] == 1


async def test_compute_approvals_empty_window_is_null(db):
    """No resolved rows → rates are None, never 0.0."""
    from genesis.eval.j9_aggregator import _compute_approvals

    metrics, sample = await _compute_approvals(
        db,
        since="2000-01-01",
        until="2100-01-01",
    )
    assert sample == 0
    assert metrics["user_resolved_rate"] is None
    assert metrics["user_resolved_rate_excl_churn"] is None


async def test_compute_approvals_mixed_resolved_at_formats(db):
    """resolved_at carries space-separated (SQLite datetime('now')) AND
    ISO-T (+00:00) formats. Lexicographically ' ' < 'T', so a space-format
    timestamp AFTER an ISO-T window bound would leak in — datetime()
    normalization must window both formats correctly."""
    from genesis.eval.j9_aggregator import _compute_approvals

    # in-window, one of each format
    await _approval(
        db,
        "b1",
        status="approved",
        resolved_at="2026-06-05 12:00:00",
        resolved_by="telegram:button:1",
    )
    await _approval(
        db,
        "b2",
        status="approved",
        resolved_at="2026-06-06T12:00:00+00:00",
        resolved_by="dashboard",
    )
    # space-format AFTER the until bound — lexicographic compare against
    # '2026-06-30T00:00:00+00:00' would wrongly include it (' ' < 'T')
    await _approval(
        db, "b3", status="approved", resolved_at="2026-06-30 12:00:00", resolved_by="dashboard"
    )

    metrics, _ = await _compute_approvals(
        db,
        since="2026-06-01T00:00:00+00:00",
        until="2026-06-30T00:00:00+00:00",
    )
    assert metrics["total_resolved"] == 2  # b3 excluded
    assert metrics["user_resolved"] == 2


# ── WS-1 A2: goals dimension ─────────────────────────────────────────────────


async def test_compute_goal_completion_zero_terminal_is_null(db):
    """0 terminal goals → completion_rate is None (never 0.0 on 0/0), with the
    scaffold note present."""
    from genesis.db.crud import user_goals
    from genesis.eval.j9_aggregator import _compute_goal_completion

    await user_goals.create(db, title="g1", category="project")
    await user_goals.create(db, title="g2", category="learning")

    metrics, sample = await _compute_goal_completion(
        db,
        since="2000-01-01",
        until="2100-01-01",
    )
    assert sample == 2
    assert metrics["total_goals"] == 2
    assert metrics["terminal_count"] == 0
    assert metrics["completion_rate"] is None
    assert "note" in metrics


async def test_compute_goal_completion_rate(db):
    """achieved / terminal with achieved and abandoned reported separately."""
    from genesis.db.crud import user_goals
    from genesis.eval.j9_aggregator import _compute_goal_completion

    g1 = await user_goals.create(db, title="g1", category="project")
    g2 = await user_goals.create(db, title="g2", category="project")
    await user_goals.create(db, title="g3", category="project")
    await user_goals.mark_achieved(db, g1)
    await user_goals.mark_abandoned(db, g2)

    metrics, _ = await _compute_goal_completion(
        db,
        since="2000-01-01",
        until="2100-01-01",
    )
    assert metrics["by_status"] == {
        "active": 1,
        "paused": 0,
        "achieved": 1,
        "abandoned": 1,
    }
    assert metrics["terminal_count"] == 2
    assert metrics["achieved_count"] == 1
    assert metrics["abandoned_count"] == 1
    assert metrics["completion_rate"] == 0.5
    assert metrics["achieved_in_period"] == 1
    # No abandoned_in_period: no abandonment timestamp exists (updated_at is
    # refreshed by ANY edit) — the stock abandoned_count carries the signal.
    assert "abandoned_in_period" not in metrics
    assert "note" not in metrics


# ── WS-1 A2: noise/passivity dimension ───────────────────────────────────────


async def test_compute_noise_passivity(db):
    """Funnel gauges + windowed ego-cycle/proposal flows, raw buckets only."""
    from genesis.eval.j9_aggregator import _compute_noise_passivity

    old = "2020-01-01T00:00:00+00:00"
    # follow-ups: one stale-pending (leak), one completed
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, strategy, status, created_at) "
        "VALUES ('f1','session_retro','x','ego_judgment','pending',?)",
        (old,),
    )
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, strategy, status, created_at) "
        "VALUES ('f2','session_retro','y','ego_judgment','completed',?)",
        (old,),
    )
    # observation: unresolved, un-actuated, old → stale leak
    await db.execute(
        "INSERT INTO observations "
        "(id, source, type, content, priority, created_at, influenced_action, resolved, surfaced_count) "
        "VALUES ('o1','s','generic','c','medium',?,0,0,0)",
        (old,),
    )
    # ego proposals: rejected + withdrawn (decision buckets window on
    # resolved_at, which the reject/table/withdraw transitions set) +
    # stale-pending, one realist amend
    await db.execute(
        "INSERT INTO ego_proposals "
        "(id, action_type, content, status, created_at, resolved_at, realist_verdict) "
        "VALUES ('p1','investigate','c','rejected',?,?,'amend')",
        (old, old),
    )
    await db.execute(
        "INSERT INTO ego_proposals (id, action_type, content, status, created_at, resolved_at) "
        "VALUES ('p2','outreach','c','withdrawn',?,?)",
        (old, old),
    )
    await db.execute(
        "INSERT INTO ego_proposals (id, action_type, content, status, created_at) "
        "VALUES ('p3','maintenance','c','pending',?)",
        (old,),
    )
    # ego cycles: 2 empty, 1 productive
    for cid, n in (("c1", 0), ("c2", 0), ("c3", 2)):
        await db.execute(
            "INSERT INTO ego_cycle_outcomes (cycle_id, focus_type, num_proposals, created_at) "
            "VALUES (?, 'signal', ?, ?)",
            (cid, n, old),
        )
    await db.commit()

    metrics, sample = await _compute_noise_passivity(
        db,
        since="2000-01-01T00:00:00+00:00",
        until="2100-01-01T00:00:00+00:00",
    )
    assert metrics["stale_followups"] == 1
    assert metrics["followups_pending"] == 1
    assert metrics["observations_stale_unactuated"] == 1
    assert metrics["proposals_pending_stale"] == 1
    assert metrics["ego_cycles"] == 3
    assert metrics["empty_ego_cycles"] == 2
    assert metrics["empty_ego_cycle_pct"] == pytest.approx(2 / 3, abs=1e-3)
    assert metrics["proposals_in_period"] == 3
    assert metrics["rejected_count"] == 1
    assert metrics["withdrawn_count"] == 1
    assert metrics["rejected_by_action_type"] == {"investigate": 1}
    assert metrics["realist_amend"] == 1
    assert metrics["realist_reject"] == 0
    assert sample == 6  # 3 cycles + 3 proposals


async def test_compute_noise_passivity_zero_cycles_is_null(db):
    """No ego cycles in window → empty_ego_cycle_pct is None, not 0.0."""
    from genesis.eval.j9_aggregator import _compute_noise_passivity

    metrics, _ = await _compute_noise_passivity(
        db,
        since="2000-01-01T00:00:00+00:00",
        until="2100-01-01T00:00:00+00:00",
    )
    assert metrics["empty_ego_cycle_pct"] is None


# ── WS-1 A2: registration / E2E guard ────────────────────────────────────────


async def test_run_weekly_aggregation_writes_new_dimensions(db):
    """run_weekly_aggregation swallows per-dimension exceptions (a broken
    compute fn fails SILENTLY in production) — so this asserts every
    registered dimension actually produced a snapshot on a fresh DB, and that
    the memory snapshot carries the new precision_at_3 keys."""
    from genesis.eval.j9_aggregator import run_weekly_aggregation

    results = await run_weekly_aggregation(db)

    expected = {
        "memory",
        "system",
        "ego",
        "cognitive",
        "procedure",
        "cognitive_drift",
        "approvals",
        "goals",
        "noise",
        "dev_quality",
    }
    missing = expected - results.keys()
    assert not missing, f"dimensions failed silently: {missing}"

    for dim in ("approvals", "goals", "noise"):
        snap = await j9_eval.get_latest_snapshot(db, dimension=dim)
        assert snap is not None, f"no snapshot written for {dim}"

    memory_snap = await j9_eval.get_latest_snapshot(db, dimension="memory")
    assert "precision_at_3" in memory_snap["metrics"]
    assert "judge_prompt_versions" in memory_snap["metrics"]


async def test_noise_decision_buckets_window_on_resolved_at(db):
    """A proposal created BEFORE the window but decided INSIDE it is this
    week's decision — created_at windowing would undercount boundary-
    straddling proposals (Codex P2 on PR #966)."""
    from genesis.eval.j9_aggregator import _compute_noise_passivity

    # created long before the window, rejected inside it
    await db.execute(
        "INSERT INTO ego_proposals (id, action_type, content, status, created_at, resolved_at) "
        "VALUES ('pb1','investigate','c','rejected',"
        "'1999-01-01T00:00:00+00:00','2050-06-01T00:00:00+00:00')",
    )
    # created AND resolved before the window — must NOT count
    await db.execute(
        "INSERT INTO ego_proposals (id, action_type, content, status, created_at, resolved_at) "
        "VALUES ('pb2','outreach','c','withdrawn',"
        "'1999-01-01T00:00:00+00:00','1999-06-01T00:00:00+00:00')",
    )
    await db.commit()

    metrics, _ = await _compute_noise_passivity(
        db,
        since="2000-01-01T00:00:00+00:00",
        until="2100-01-01T00:00:00+00:00",
    )
    assert metrics["rejected_count"] == 1  # pb1: decided in window
    assert metrics["withdrawn_count"] == 0  # pb2: decided before window
    assert metrics["proposals_in_period"] == 0  # neither was CREATED in window
    assert metrics["rejected_by_action_type"] == {"investigate": 1}


async def test_j9_eval_status_reports_new_snapshot_dimensions(db, monkeypatch):
    """The MCP health endpoint must surface the three snapshot-only dims
    (they have no eval_events, so only the snapshot loop shows them)."""
    from types import SimpleNamespace

    import genesis.mcp.health_mcp as health_mcp_mod
    from genesis.eval.j9_aggregator import run_weekly_aggregation
    from genesis.mcp.health.j9_eval import _impl_j9_eval_status

    await run_weekly_aggregation(db)
    monkeypatch.setattr(health_mcp_mod, "_service", SimpleNamespace(_db=db))

    res = await _impl_j9_eval_status()
    for dim in ("approvals", "goals", "noise"):
        assert dim in res["latest_snapshots"], f"missing {dim}"
        assert res["latest_snapshots"][dim] is not None


async def test_compute_goal_completion_excludes_ego_goals(db):
    """PR-3a: origin='genesis_ego' goals must not contaminate the user-goal
    eval signal — neither the status ratios nor the achieved window."""
    from genesis.db.crud import user_goals
    from genesis.eval.j9_aggregator import _compute_goal_completion

    u1 = await user_goals.create(db, title="user g", category="project")
    e1 = await user_goals.create(
        db,
        title="ego g",
        category="project",
        origin="genesis_ego",
    )
    await user_goals.create(
        db,
        title="ego g2",
        category="project",
        origin="genesis_ego",
    )
    await user_goals.mark_achieved(db, u1)
    await user_goals.mark_achieved(db, e1)

    metrics, sample = await _compute_goal_completion(
        db,
        since="2000-01-01",
        until="2100-01-01",
    )
    assert sample == 1  # the user goal only
    assert metrics["total_goals"] == 1
    assert metrics["by_status"] == {
        "active": 0,
        "paused": 0,
        "achieved": 1,
        "abandoned": 0,
    }
    assert metrics["achieved_count"] == 1


# ── No-data scoring: a factor without a denominator must not score as 0 ──────
#
# Acceptance bar for the 2026-09-06 ego "F" (score 4.0). That week had 12
# proposals: 11 still pending, and ONE tabled 60ms after creation by the
# develop-scope roadmap gate (a recoverable bookkeeping record of a proposal
# self-development being disabled declined to pursue). The tabled row became
# the entire resolved population, so approval_rate was 0/1, its 0.85 confidence
# scored against a 0.0 outcome gave calibration 0.1, and a null
# execution_success_rate was zero-filled at 40% weight. No human judged
# anything, yet the ego was graded F on it.


async def _seed_window_proposals(db, rows):
    """Seed ego_proposals. rows = [(status, confidence, resolved)]."""
    for i, (status, conf, resolved) in enumerate(rows):
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, content, status, confidence, created_at, resolved_at) "
            "VALUES (?,'maintenance','c',?,?,?,?)",
            (f"np{i}", status, conf, "2026-09-02T00:00:00+00:00",
             "2026-09-02T00:00:00+00:00" if resolved else None),
        )
    await db.commit()


async def test_ego_grade_withheld_when_only_resolution_is_a_self_tabling(db):
    """The real 2026-09-06 defect: 11 pending + 1 auto-tabled must NOT grade F.

    Nobody judged any of these proposals, so there is nothing to grade. The
    correct outcome is a WITHHELD grade, not the worst possible one.
    """
    from genesis.eval.j9_aggregator import _compute_ego_quality, _grade_ego

    await _seed_window_proposals(
        db,
        [("pending", 0.75, False)] * 11 + [("tabled", 0.85, True)],
    )
    since, until = "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00"
    metrics, _ = await _compute_ego_quality(db, since, until)
    result = await _grade_ego(db, since, until, {"ego": metrics})

    assert result["grade"] != "F", (
        "an ego whose proposals nobody answered was graded F on one "
        f"self-tabled bookkeeping row: {result}"
    )
    assert result["grade"] is None, result


async def test_tabled_and_withdrawn_are_not_rejections(db):
    """Non-judgments must stay out of the approval denominator.

    Both statuses are reachable with no human involved — `auto_table_stale_
    proposals` and the ego's own reconcile withdrawal — so scoring them as
    rejections penalises the ego for its own cleanup.
    """
    from genesis.eval.j9_aggregator import _compute_ego_quality

    await _seed_window_proposals(
        db,
        [("approved", 0.8, True), ("rejected", 0.8, True),
         ("tabled", 0.8, True), ("withdrawn", 0.8, True)],
    )
    metrics, _ = await _compute_ego_quality(
        db, "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
    )
    # 1 approved of (1 approved + 1 rejected) — tabled/withdrawn excluded.
    assert metrics["approval_rate"] == 0.5, metrics
    assert metrics["total_resolved"] == 2, metrics


async def test_ego_min_samples_counts_the_scored_population(db):
    """The sufficiency gate must read the population the factors came from.

    Pending proposals contribute to no factor, so they must not be what
    satisfies _MIN_SAMPLES — otherwise a grade issues off n=1 while
    reporting a sample of 12.
    """
    from genesis.eval.j9_aggregator import _compute_ego_quality, _grade_ego

    await _seed_window_proposals(
        db, [("pending", 0.75, False)] * 20 + [("approved", 0.9, True)],
    )
    since, until = "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00"
    metrics, _ = await _compute_ego_quality(db, since, until)
    result = await _grade_ego(db, since, until, {"ego": metrics})

    assert result["grade"] is None, (
        f"graded on 1 resolved proposal padded by 20 pending ones: {result}"
    )
    assert result["sample_count"] == 1, result


async def test_absent_factor_is_excluded_not_scored_as_zero(db):
    """A factor with no denominator is dropped and the rest renormalised."""
    from genesis.eval.j9_aggregator import _score_available_factors

    all_present = [(0.8, 0.4), (0.6, 0.3), (0.7, 0.3)]
    assert abs(_score_available_factors(all_present, max_absent=1) - 71.0) < 0.01

    # Absent middle factor: (0.8*0.4 + 0.7*0.3) / (0.4+0.3) * 100 ≈ 75.71
    one_absent = [(0.8, 0.4), (None, 0.3), (0.7, 0.3)]
    assert abs(_score_available_factors(one_absent, max_absent=1) - 75.71) < 0.01

    # Too little left to judge on.
    two_absent = [(0.8, 0.4), (None, 0.3), (None, 0.3)]
    assert _score_available_factors(two_absent, max_absent=1) is None


async def test_absent_factor_never_drags_a_grade_downward(db):
    """Regression guard: absence must never be cheaper than presence.

    Zero-filling made 'no data' indistinguishable from 'scored 0.0', which is
    the mechanism behind both recorded ego F grades.
    """
    from genesis.eval.j9_aggregator import _score_available_factors

    absent = _score_available_factors([(0.8, 0.4), (None, 0.6)], max_absent=1)
    scored_zero = _score_available_factors([(0.8, 0.4), (0.0, 0.6)], max_absent=1)
    assert absent > scored_zero, (absent, scored_zero)


async def test_procedural_grade_gated_on_the_outcome_population(db):
    """The gate must read outcomes, not invocations.

    success_rate is 50% of the procedural score and is computed over
    procedure_outcome events, while the sufficiency gate read
    invocation_count — a different, independently-sized population. A week
    with plenty of invocations and one outcome graded on that one outcome.
    """
    dimension_results = {
        "procedure": {
            "success_rate": 0.0,
            "mean_confidence": 0.7,
            "invocation_count": 435,   # clears the old gate easily
            "outcome_count": 1,        # ...on a single measured outcome
            "total_procedures": 2334,
        },
    }
    result = await _grade_procedural(db, "2026-05-01", "2026-05-08", dimension_results)
    assert result["grade"] is None, result
    assert "outcomes" in result.get("reason", ""), result


async def test_awareness_and_reflection_scoring_is_unchanged(db):
    """These two graders never produce an absent factor, so the helper change
    must be a no-op for them.

    Their factors are ratios that default to 0.0 rather than None once the
    gate passes, so renormalising over 'present' factors is the identity. This
    pins that the blast radius of the scoring change stops here.
    """
    from genesis.eval.j9_aggregator import _score_available_factors

    # Real weight vectors: awareness (.6/.4) and reflection (.25/.5/.25).
    # An earlier revision of this test used memory's (.4/.3/.3) by mistake and
    # claimed to cover reflection.
    for weights in ([(0.9, 0.6), (0.8, 0.4)],
                    [(1.0, 0.25), (0.55, 0.5), (0.6, 0.25)]):
        renormalised = _score_available_factors(weights, max_absent=0)
        legacy = sum(v * w for v, w in weights) * 100
        assert abs(renormalised - legacy) < 1e-9, (weights, renormalised, legacy)


async def test_calibration_buckets_cover_every_confidence_exactly_once(db):
    """Bucket edges must not be accumulated in floating point.

    `bucket_low + 0.2` gives 0.4 + 0.2 == 0.6000000000000001, which put a
    confidence of exactly 0.6 in TWO buckets and one of exactly 1.0 in NONE.
    Calibration is 40% of the ego grade, and `j9_regression` proposals are
    created at confidence 1.0 — so every auto-filed regression proposal was
    invisible to the heaviest factor in its own subsystem's grade.
    """
    from genesis.eval.j9_aggregator import _CALIBRATION_BUCKETS, _compute_ego_quality

    for lo, hi in _CALIBRATION_BUCKETS:
        assert round(hi - lo, 10) == 0.2, (lo, hi)

    # NOTE: a total-slot count is NOT a valid assertion here — under the bug
    # the double-counted 0.6 and the dropped 1.0 cancel out exactly, so the
    # total is conserved. Assert each edge case's PLACEMENT instead.
    await _seed_window_proposals(db, [("approved", 0.6, True)])
    metrics, _ = await _compute_ego_quality(
        db, "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
    )
    cal = metrics["confidence_calibration"]
    assert sum(b["count"] for b in cal.values()) == 1, (
        f"one proposal at confidence 0.6 occupies {len(cal)} buckets: {cal}"
    )
    assert list(cal) == ["0.6-0.8"], cal


async def test_calibration_counts_a_confidence_of_exactly_one(db):
    """confidence == 1.0 fell outside every half-open bucket.

    j9_regression proposals are created at confidence 1.0
    (regression_alert.py), so the ego's own auto-filed regressions never
    reached the factor worth 40% of its grade.
    """
    from genesis.eval.j9_aggregator import _compute_ego_quality

    await _seed_window_proposals(db, [("approved", 1.0, True)])
    metrics, _ = await _compute_ego_quality(
        db, "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
    )
    cal = metrics["confidence_calibration"]
    assert cal.get("0.8-1.0", {}).get("count") == 1, (
        f"a confidence of exactly 1.0 was counted nowhere: {cal}"
    )


async def test_ego_metrics_carry_the_approval_rate_definition_marker(db):
    """approval_rate is the ego headline series — its definition must travel.

    It drives the dashboard sparkline and _extract_trend_values -> ego_slope
    -> go_ready. Excluding tabled/withdrawn shrinks the denominator and raises
    the rate, so an unmarked change would read as improvement.
    """
    from genesis.eval.j9_aggregator import (
        _EGO_APPROVAL_RATE_DEFN,
        _compute_ego_quality,
        _extract_trend_values,
    )

    await _seed_window_proposals(db, [("approved", 0.8, True)])
    metrics, _ = await _compute_ego_quality(
        db, "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
    )
    assert metrics["approval_rate_defn"] == _EGO_APPROVAL_RATE_DEFN

    # A pre-marker snapshot must not be averaged into the slope.
    mixed = [{"metrics": {"approval_rate": 0.1}},                       # v1
             {"metrics": {"approval_rate": 0.9,
                          "approval_rate_defn": _EGO_APPROVAL_RATE_DEFN}}]
    assert _extract_trend_values("ego", mixed) == [0.9]
    # Other dimensions are unaffected by the guard.
    assert _extract_trend_values(
        "memory", [{"metrics": {"precision_at_5": 0.5}}]) == [0.5]


async def test_withheld_grade_reason_reaches_the_dashboard_key(db):
    """`reason` must land in factors, which is where the dashboard reads it.

    insert_subsystem_grade has no `reason` column and
    dashboard/routes/eval.py reads factors["reason"], so every grader's
    explanation was silently dropped. This fix makes the withheld path — now
    the common path for ego — legible instead of blank.
    """
    from genesis.eval.j9_aggregator import _compute_ego_quality, _grade_ego

    await _seed_window_proposals(db, [("pending", 0.7, False)] * 6)
    since, until = "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00"
    metrics, _ = await _compute_ego_quality(db, since, until)
    info = await _grade_ego(db, since, until, {"ego": metrics})

    assert info["grade"] is None
    grade_factors = dict(info["factors"])
    if info.get("reason") and "reason" not in grade_factors:
        grade_factors["reason"] = info["reason"]          # the persist chokepoint
    assert "judged" in grade_factors["reason"], grade_factors["reason"]


async def test_a_failed_execution_still_counts_as_an_approval(db):
    """`failed` is post-approval, so it belongs in the approval numerator.

    ego_crud.execute_proposal only transitions rows that are already
    'approved', and mark_proposal_verification_failed only transitions rows
    that are already 'executed'. So a failed proposal is by definition one the
    user approved; scoring it as an approval failure conflates execution with
    approval — the same conflation the rest of this change removes. Its
    failure is measured separately by execution_success_rate.
    """
    from genesis.eval.j9_aggregator import _compute_ego_quality

    await _seed_window_proposals(
        db,
        [("approved", 0.8, True), ("executed", 0.8, True),
         ("failed", 0.8, True), ("rejected", 0.8, True)],
    )
    metrics, _ = await _compute_ego_quality(
        db, "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
    )
    # 3 of 4 judged proposals were approved; only the rejection was not.
    assert metrics["approval_rate"] == 0.75, metrics
    # The failure is still counted, in the metric that measures execution.
    assert metrics["execution_success_rate"] == 0.5, metrics
