"""J-9 subsystem-grade regression detection + human-gated surfacing.

The MONITOR half of the self-improvement control plane. J-9 computes weekly
per-subsystem quality grades (memory/ego/procedural/awareness/reflection) but
nothing consumed them — render-only telemetry. This module gives those grades
their first control path: a *regression* triggers (1) a deterministic BLOCKER
alert to the operator and (2) a HUMAN-GATED ``j9_regression`` ego proposal.

Neither auto-acts. The proposal is informational (NOTIFY_USER + never-dispatch):
approving it merely acknowledges; the remedy — e.g. an Evo experiment on the
regressed subsystem — is the user's call. No procedure/config is demoted or hidden
by this code.

Conservative while the grade baseline is young (~weeks): fires only on an
ABSOLUTE FLOOR (grade F) or a ≥15-point week-over-week score DROP, and never on a
cold-start week (grade None / insufficient data). Idempotent per
(subsystem, period_end) via a deterministic proposal id, so a restart-triggered
re-run of the weekly aggregation never double-files or double-alerts.
"""

from __future__ import annotations

import hashlib
import logging

import aiosqlite

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import j9_eval

logger = logging.getLogger(__name__)

# Must match j9_regression_actions.J9_REGRESSION_ACTION_TYPE (kept local to avoid
# an eval→ego import; the handler module is the canonical owner).
_J9_REGRESSION_ACTION_TYPE = "j9_regression"

# Conservative thresholds for a young grade baseline. Absolute floor = F only (a
# D at 5-6 samples can be measurement noise); delta = a ≥15-point week-over-week
# score drop regardless of grade. Tunable down toward D once variance is known.
_ABSOLUTE_FLOOR_GRADE = "F"
_DELTA_DROP_POINTS = 15.0


def _proposal_id(subsystem: str, period_end: str) -> str:
    """Deterministic id from (subsystem, period_end) — the idempotency key."""
    return hashlib.sha256(
        f"{_J9_REGRESSION_ACTION_TYPE}:{subsystem}:{period_end}".encode(),
    ).hexdigest()[:16]


def _regression_reason(latest: dict, prior: dict | None) -> str | None:
    """Return a human reason string if *latest* is a regression, else None.

    Guards on ``grade is not None`` — a cold-start / insufficient-data week
    (grade None) never alerts. Two triggers: absolute floor (grade F) or a
    ≥15-point week-over-week score drop.
    """
    grade = latest.get("grade")
    if not grade:  # None / "" → insufficient data, never alert
        return None
    score = latest.get("score")
    if grade == _ABSOLUTE_FLOOR_GRADE:
        score_txt = f" (score {score:.0f})" if isinstance(score, (int, float)) else ""
        return f"grade {grade}{score_txt}"
    prior_score = prior.get("score") if prior else None
    if isinstance(score, (int, float)) and isinstance(prior_score, (int, float)):
        drop = prior_score - score
        if drop >= _DELTA_DROP_POINTS:
            return (
                f"score dropped {drop:.0f} pts week-over-week "
                f"({prior_score:.0f}→{score:.0f}), grade {grade}"
            )
    return None


async def check_and_alert_regressions(
    db: aiosqlite.Connection,
    outreach_pipeline: object | None = None,
) -> list[dict]:
    """Detect weekly subsystem-grade regressions and surface them (human-gated).

    For each regressed subsystem: send a BLOCKER alert (if a pipeline is given)
    and file an informational ``j9_regression`` proposal. Idempotent per
    (subsystem, period_end) — the proposal's existence marks the period handled.
    Returns the list of regressions handled (for logging/tests). Never raises into
    the caller; per-subsystem failures are logged and skipped.
    """
    handled: list[dict] = []
    try:
        grades = await j9_eval.get_latest_subsystem_grades(db, period_type="weekly")
    except Exception:
        logger.warning("j9 regression check: failed to read grades", exc_info=True)
        return handled

    for latest in grades:
        sub = latest.get("subsystem")
        period_end = latest.get("period_end")
        if not sub or not period_end:
            continue

        # Prior week for the delta check (limit=2 → [latest, prior]).
        prior = None
        try:
            hist = await j9_eval.get_subsystem_grades(
                db, subsystem=sub, period_type="weekly", limit=2,
            )
            if len(hist) >= 2:
                prior = hist[1]
        except Exception:
            logger.warning(
                "j9 regression check: history read failed for %s", sub, exc_info=True,
            )

        reason = _regression_reason(latest, prior)
        if not reason:
            continue

        pid = _proposal_id(sub, period_end)
        # Idempotency: the proposal's existence marks this (subsystem, period) as
        # already handled — a restart re-run skips BOTH alert and proposal.
        try:
            if await ego_crud.get_proposal(db, pid):
                continue
        except Exception:
            logger.warning(
                "j9 regression check: existence check failed for %s", sub, exc_info=True,
            )
            continue

        text = (
            f"Cognitive subsystem regression — {sub}: {reason} "
            f"(week ending {period_end[:10]}). Investigate the {sub} pipeline; "
            f"candidate remedy: an Evo experiment on the {sub} config."
        )

        # 1. BLOCKER alert (best-effort; governance dedups within 168h).
        if outreach_pipeline is not None:
            try:
                from genesis.outreach.types import OutreachCategory, OutreachRequest

                await outreach_pipeline.submit_raw(
                    text,
                    OutreachRequest(
                        category=OutreachCategory.BLOCKER,
                        topic=f"Eval regression: {sub}",
                        context=text,
                        salience_score=1.0,
                        signal_type="j9_regression_alert",
                        source_id=f"j9_regression:{sub}:{period_end[:10]}",
                    ),
                )
            except Exception:
                logger.warning("j9 regression alert failed for %s", sub, exc_info=True)

        # 2. Human-gated informational proposal (durable idempotent marker).
        try:
            await ego_crud.create_proposal(
                db,
                id=pid,
                action_type=_J9_REGRESSION_ACTION_TYPE,
                content=text,
                rationale=(
                    "J-9 weekly eval flagged a cognitive-subsystem quality "
                    "regression. Recommend-only: approving acknowledges it; no "
                    "automated remediation runs."
                ),
                confidence=1.0,
                urgency="high",
                status="pending",
                ego_source="j9_eval",
            )
        except Exception:
            logger.warning(
                "j9 regression proposal creation failed for %s", sub, exc_info=True,
            )
            continue

        handled.append(
            {"subsystem": sub, "period_end": period_end, "reason": reason, "proposal_id": pid},
        )

    if handled:
        logger.info(
            "J9 regression check: %d subsystem regression(s) surfaced", len(handled),
        )
    return handled
