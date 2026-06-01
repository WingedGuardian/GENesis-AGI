"""Lightweight eval event emitters for J-9 instrumentation.

Each function is fire-and-forget — errors are logged but never propagate
to the caller. This ensures eval instrumentation cannot break production
code paths.
"""

from __future__ import annotations

import logging

import aiosqlite

from genesis.db.crud import j9_eval

logger = logging.getLogger(__name__)


async def emit_recall_fired(
    db: aiosqlite.Connection,
    *,
    query: str,
    result_count: int,
    top_scores: list[float],
    memory_ids: list[str],
    latency_ms: float,
    source: str,
    session_id: str | None = None,
    mode: str | None = None,
    pipeline_used: str | None = None,
    intent_category: str | None = None,
    graph_boost_applied: bool = False,
    mean_score: float | None = None,
    wing: str | None = None,
) -> str | None:
    """Log a memory recall() invocation as an eval event.

    Returns the event id so callers can link diagnostics events.
    """
    try:
        metrics: dict = {
            "query": query[:500],
            "result_count": result_count,
            "top_scores": top_scores[:10],
            "memory_ids": memory_ids[:10],
            "latency_ms": round(latency_ms, 1),
            "source": source,
            "graph_boost_applied": graph_boost_applied,
        }
        if mode:
            metrics["mode"] = mode
        if pipeline_used:
            metrics["pipeline_used"] = pipeline_used
        if intent_category:
            metrics["intent_category"] = intent_category
        if mean_score is not None:
            metrics["mean_score"] = round(mean_score, 4)
        if wing:
            metrics["wing"] = wing
        return await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_fired",
            session_id=session_id,
            metrics=metrics,
        )
    except Exception:
        logger.debug("eval: failed to emit recall_fired", exc_info=True)
        return None


async def emit_proposal_resolved(
    db: aiosqlite.Connection,
    *,
    proposal_id: str,
    status: str,
    confidence: float | None = None,
    action_type: str | None = None,
) -> None:
    """Log an ego proposal resolution (approved/rejected/etc)."""
    try:
        await j9_eval.insert_event(
            db,
            dimension="ego",
            event_type="proposal_resolved",
            subject_id=proposal_id,
            metrics={
                "status": status,
                "confidence": confidence,
                "action_type": action_type,
            },
        )
    except Exception:
        logger.debug("eval: failed to emit proposal_resolved", exc_info=True)


async def emit_procedure_invoked(
    db: aiosqlite.Connection,
    *,
    procedure_id: str,
    confidence: float,
    matched_tags: list[str],
    session_id: str | None = None,
) -> None:
    """Log a procedure recall invocation."""
    try:
        await j9_eval.insert_event(
            db,
            dimension="procedure",
            event_type="procedure_invoked",
            subject_id=procedure_id,
            session_id=session_id,
            metrics={
                "confidence_at_invoke": confidence,
                "matched_tags": matched_tags[:10],
            },
        )
    except Exception:
        logger.debug("eval: failed to emit procedure_invoked", exc_info=True)


async def emit_procedure_outcome(
    db: aiosqlite.Connection,
    *,
    procedure_id: str,
    success: bool,
    confidence_after: float,
) -> None:
    """Log a procedure success/failure outcome."""
    try:
        await j9_eval.insert_event(
            db,
            dimension="procedure",
            event_type="procedure_outcome",
            subject_id=procedure_id,
            metrics={
                "success": success,
                "confidence_after": confidence_after,
            },
        )
    except Exception:
        logger.debug("eval: failed to emit procedure_outcome", exc_info=True)


async def emit_gate_decision(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    outcome: str,
    allowed: bool,
    confidence: float,
    flags: list[str],
) -> None:
    """Log a validation gate decision for procedural extraction."""
    try:
        await j9_eval.insert_event(
            db,
            dimension="procedure",
            event_type="gate_decision",
            metrics={
                "task_type": task_type,
                "outcome": outcome,
                "allowed": allowed,
                "confidence": confidence,
                "flags": flags[:5],
            },
        )
    except Exception:
        logger.debug("eval: failed to emit gate_decision", exc_info=True)


async def emit_recall_diagnostics(
    db: aiosqlite.Connection,
    *,
    recall_event_id: str | None,
    qdrant_pool_size: int,
    fts_pool_size: int,
    event_pool_size: int,
    total_candidates: int,
    overlap_count: int,
    score_spread: float | None,
    embedding_available: bool,
    intent_category: str,
    intent_confidence: float,
    query_expanded: bool,
) -> None:
    """Log intermediate retrieval pipeline diagnostics for a recall.

    Captures source pool sizes, overlap statistics, and RRF score
    distribution — data that exists in local variables during recall()
    but otherwise goes out of scope.
    """
    try:
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_diagnostics",
            subject_id=recall_event_id,
            metrics={
                "qdrant_pool": qdrant_pool_size,
                "fts_pool": fts_pool_size,
                "event_pool": event_pool_size,
                "total_candidates": total_candidates,
                "overlap": overlap_count,
                "score_spread": score_spread,
                "embedding_available": embedding_available,
                "intent": intent_category,
                "intent_confidence": round(intent_confidence, 3),
                "query_expanded": query_expanded,
            },
        )
    except Exception:
        logger.debug("eval: failed to emit recall_diagnostics", exc_info=True)


async def emit_recall_used(
    db: aiosqlite.Connection,
    *,
    memory_ids: list[str],
    source: str = "memory_expand",
) -> None:
    """Log that specific memories were accessed/used downstream.

    This provides the implicit relevance signal that
    _compute_memory_quality() in j9_aggregator expects from
    'recall_used' events.
    """
    try:
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_used",
            metrics={
                "memory_ids": memory_ids[:20],
                "source": source,
                "used": True,
                "count": len(memory_ids),
            },
        )
    except Exception:
        logger.debug("eval: failed to emit recall_used", exc_info=True)


async def emit_calibration_ece(
    db: aiosqlite.Connection,
    *,
    domain: str,
    ece: float,
    sample_count: int,
) -> str | None:
    """Emit calibration ECE metric (Verified Autonomy L4)."""
    try:
        return await j9_eval.insert_event(
            db,
            dimension="system",
            event_type="calibration_ece",
            metrics={
                "domain": domain,
                "ece": ece,
                "sample_count": sample_count,
            },
        )
    except Exception:
        logger.debug("eval: failed to emit calibration_ece", exc_info=True)
        return None
