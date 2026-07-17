"""WS-2 P1b — fire-and-forget commit-path writer hooks.

Coverage is mandatory by construction: every action in a starter class writes
its falsifiable prediction(s) at the moment it commits — hooks are code in the
commit path, not LLM discipline. The contract mirrors
``feedback/bus.py::record_outcome``: a hook NEVER raises, never blocks or
delays the action, never touches transaction state on the shared connection.

Failures are counted per action_class (``write_failure_counts`` is read by
``mcp/health/errors._compute_alerts``, which emits
``ledger:write_failed:<class>`` through the M10 awareness-tick alert writer —
the single designated ``alert_events`` persister) and ``logger.error``-ed.
A dedupe ``IntegrityError`` is debug-only, NOT counted: retries and resends
legitimately re-enter commit paths, and the CRUD validates everything else
before its INSERT, so an IntegrityError from this path is the UNIQUE key.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from genesis.ledger.metrics import TASK_GRACE

logger = logging.getLogger(__name__)

# ── policy_prior seeds (user decision 2026-07-16: base rates, not flat 0.5) ──
# Outreach seeds are measured, not guessed: the 2026-07-16 spike found 7/1026
# affirmative replies (0.7%) and ~20/1040 positive engagements on the live DB.
# Seeding 0.5 would make the prior lane maximally wrong on day one; the
# calibration cells (P3) take over as graded data accumulates. Task/ego lanes
# have no measured base rate yet — they start at 0.5 until cells exist.
_PRIOR_SEEDS: dict[str, float] = {
    "reply_received": 0.02,
    "positive_engagement": 0.02,
    "completed": 0.5,
    "completed_first_attempt": 0.5,
    "user_greenlights": 0.5,
    "approved_and_executes": 0.5,
}

# Build-lane confidence arrives as a text label ("high"/"medium"/"low",
# inbox/recommendation.py). It IS a stated confidence, just coarse — mapped to
# floats with the original label kept in row metadata; the §2.6 specificity
# audit is the backstop if the binning misbehaves (user decision 2026-07-16).
_BUILD_LABEL_CONFIDENCE: dict[str, float] = {"high": 0.85, "medium": 0.60, "low": 0.35}

# Reply horizons in hours; 72h default, per-category overridable (locked
# design decision — populate overrides here as categories earn them).
_CATEGORY_HORIZON_H: dict[str, int] = {}
_DEFAULT_HORIZON_H = 72

# Tasks have no task-level timeout (only per-step bounds, unknowable step
# count at claim) — constant bounded-runtime allowance + grace.
_TASK_HORIZON = timedelta(hours=24) + TASK_GRACE

_write_failures: Counter[str] = Counter()


def write_failure_counts() -> dict[str, int]:
    """Per-action_class ledger-write failures since process start.

    Nonzero in the runtime process means the commit path is dropping
    predictions — surfaced as ``ledger:write_failed:<class>`` via
    ``_compute_alerts``. Restart resets the counter; the durable trail is the
    ``alert_events`` row the awareness tick persisted while it was firing.
    """
    return dict(_write_failures)


def _reset_failure_counts_for_tests() -> None:
    _write_failures.clear()


def _clamped_stated(value: Any) -> float | None:
    """A usable stated confidence, or None → policy_prior lane.

    0.0 means "absent" (the ego pipeline's default for a missing confidence),
    never a genuine stated zero; usable values clamp to the writer's
    [0.01, 0.99] domain.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f <= 0.0:
        return None
    return min(max(f, 0.01), 0.99)


async def _write_prediction(
    db: Any,
    *,
    action_class: str,
    subject_ref_type: str,
    subject_ref_id: str,
    domain: str,
    metric: str,
    confidence: float,
    provenance: str,
    deadline: datetime,
    predictor: str,
    metadata: dict | None = None,
) -> None:
    """One guarded row write — the only path hooks use. Never raises."""
    try:
        from genesis.db.crud import ledger_predictions as lp_crud

        await lp_crud.create(
            db,
            action_class=action_class,
            subject_ref_type=subject_ref_type,
            subject_ref_id=subject_ref_id,
            domain=domain,
            metric=metric,
            confidence=confidence,
            deadline_at=deadline.isoformat(),
            provenance=provenance,
            predictor=predictor,
            metadata=metadata,
        )
    except sqlite3.IntegrityError:
        # The dedupe UNIQUE key: a retry/resend re-entered the commit path.
        # Idempotent by design — the original prediction stands.
        logger.debug(
            "ledger dedupe: prediction exists for %s/%s/%s",
            action_class,
            subject_ref_id,
            metric,
        )
    except Exception:
        _write_failures[action_class] += 1
        logger.error(
            "ledger prediction write failed (%s/%s/%s) — action unaffected",
            action_class,
            subject_ref_id,
            metric,
            exc_info=True,
        )


async def on_outreach_delivered(
    db: Any,
    *,
    outreach_id: str,
    category: str,
    stated_confidence: float | None = None,
) -> None:
    """Post-delivery hook (OutreachPipeline._deliver): reply_received +
    positive_engagement predictions for the just-recorded send."""
    try:
        now = datetime.now(UTC)
        deadline = now + timedelta(hours=_CATEGORY_HORIZON_H.get(category, _DEFAULT_HORIZON_H))
        stated = _clamped_stated(stated_confidence)
        for metric in ("reply_received", "positive_engagement"):
            await _write_prediction(
                db,
                action_class="outreach_send",
                subject_ref_type="outreach",
                subject_ref_id=outreach_id,
                domain=f"outreach.{category}",
                metric=metric,
                confidence=stated if stated is not None else _PRIOR_SEEDS[metric],
                provenance="stated" if stated is not None else "policy_prior",
                deadline=deadline,
                predictor="outreach_pipeline",
            )
    except Exception:
        _write_failures["outreach_send"] += 1
        logger.error("on_outreach_delivered hook failed — send unaffected", exc_info=True)


async def on_task_claimed(
    db: Any,
    *,
    task_id: str,
    source: str,
    stated_confidence: float | None = None,
) -> None:
    """Post-atomic-claim hook (ExecutionEngine.execute, pending path only —
    resume/restart paths rely on the dedupe key)."""
    try:
        now = datetime.now(UTC)
        deadline = now + _TASK_HORIZON
        stated = _clamped_stated(stated_confidence)
        for metric in ("completed", "completed_first_attempt"):
            await _write_prediction(
                db,
                action_class="task_execution",
                subject_ref_type="task",
                subject_ref_id=task_id,
                domain=f"task.{source}",
                metric=metric,
                confidence=stated if stated is not None else _PRIOR_SEEDS[metric],
                provenance="stated" if stated is not None else "policy_prior",
                deadline=deadline,
                predictor="task_executor",
            )
    except Exception:
        _write_failures["task_execution"] += 1
        logger.error("on_task_claimed hook failed — task unaffected", exc_info=True)


async def on_build_verdict(
    db: Any,
    *,
    candidate_id: str,
    verdict: str,
    confidence_label: str | None = None,
) -> None:
    """Post-create hook for BOTH build_candidates create sites (_handle_build
    carded 'build' rows AND _record_calibration report-only rows).

    The metric is always ``user_greenlights``; a dont_build/needs_discussion
    verdict predicts the complement (the user will NOT greenlight), so its
    label confidence inverts.
    """
    try:
        now = datetime.now(UTC)
        label = (confidence_label or "").strip().lower()
        mapped = _BUILD_LABEL_CONFIDENCE.get(label)
        if mapped is not None:
            confidence = mapped if verdict == "build" else round(1.0 - mapped, 2)
            provenance = "stated"
        else:
            confidence = _PRIOR_SEEDS["user_greenlights"]
            provenance = "policy_prior"
        await _write_prediction(
            db,
            action_class="build_verdict",
            subject_ref_type="build_candidate",
            subject_ref_id=candidate_id,
            domain="build",
            metric="user_greenlights",
            confidence=confidence,
            provenance=provenance,
            deadline=now + timedelta(days=7),
            predictor="build_lane",
            metadata={"confidence_label": confidence_label, "verdict": verdict},
        )
    except Exception:
        _write_failures["build_verdict"] += 1
        logger.error("on_build_verdict hook failed — build lane unaffected", exc_info=True)


async def on_ego_proposal(
    db: Any,
    *,
    proposal_id: str,
    action_type: str,
    confidence: float | None = None,
) -> None:
    """Per-proposal hook (EgoProposalPipeline.create_batch loop). The 0.0
    default of a missing proposal confidence reads as absent → policy_prior."""
    try:
        now = datetime.now(UTC)
        stated = _clamped_stated(confidence)
        await _write_prediction(
            db,
            action_class="ego_proposal",
            subject_ref_type="proposal",
            subject_ref_id=proposal_id,
            domain=f"ego.{action_type}",
            metric="approved_and_executes",
            confidence=stated if stated is not None else _PRIOR_SEEDS["approved_and_executes"],
            provenance="stated" if stated is not None else "policy_prior",
            deadline=now + timedelta(days=7),
            predictor="ego_pipeline",
        )
    except Exception:
        _write_failures["ego_proposal"] += 1
        logger.error("on_ego_proposal hook failed — proposal unaffected", exc_info=True)
