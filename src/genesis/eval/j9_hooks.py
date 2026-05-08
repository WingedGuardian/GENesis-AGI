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
) -> None:
    """Log a memory recall() invocation as an eval event."""
    try:
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="recall_fired",
            session_id=session_id,
            metrics={
                "query": query[:500],
                "result_count": result_count,
                "top_scores": top_scores[:10],
                "memory_ids": memory_ids[:10],
                "latency_ms": round(latency_ms, 1),
                "source": source,
            },
        )
    except Exception:
        logger.debug("eval: failed to emit recall_fired", exc_info=True)


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
