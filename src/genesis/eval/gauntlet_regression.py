"""Advisory regression surfacing for the model-roster gauntlet.

Gating is ADVISORY (plan Phase 6): a gauntlet regression NEVER auto-removes a
model from selection/failover. It (1) sends a BLOCKER alert and (2) files a
human-gated ``gauntlet_regression`` ego proposal (informational — approving it
merely acknowledges; the handler in ``ego/gauntlet_regression_actions`` marks it
executed, and it is blocklisted from dispatch).

A "regression" is specifically a PASS→FAIL transition: the just-completed run
genuinely FAILED (>=1 non-skipped fixture failed) AND a prior run for the same
model PASSED (all attempted fixtures green). A model that never passed (cold
start) or an all-skipped / inconclusive run (e.g. a peer out of balance → every
fixture SKIPPED) is NOT a regression and never alerts.

Mirrors the J-9 ``regression_alert`` pattern; kept separate for now (unifying the
BLOCKER-alert+proposal helper is a tracked follow-up).
"""
from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from genesis.eval.db import get_runs

if TYPE_CHECKING:
    import aiosqlite

    from genesis.eval.types import EvalRunSummary

logger = logging.getLogger(__name__)

_ACTION_TYPE = "gauntlet_regression"
_DATASET = "gauntlet"
# How many prior runs to scan for a "was it ever trusted" PASS.
_PRIOR_SCAN_LIMIT = 25


def _is_pass(passed: int, failed: int) -> bool:
    return failed == 0 and passed > 0


def _proposal_id(model_id: str, run_id: str) -> str:
    """Deterministic idempotency key from (model, run)."""
    return hashlib.sha256(f"{_ACTION_TYPE}:{model_id}:{run_id}".encode()).hexdigest()[:16]


async def check_gauntlet_regression(
    db: aiosqlite.Connection,
    summary: EvalRunSummary,
    outreach_pipeline: object | None = None,
) -> dict | None:
    """Surface a gauntlet PASS→FAIL regression (advisory). Never raises.

    Returns the handled-regression dict, or None when it is not a regression
    (inconclusive / not-a-fail / cold-start / already-handled). Assumes the run
    in *summary* has already been persisted (its own row is excluded via id).
    """
    try:
        passed = summary.passed_cases
        failed = summary.failed_cases

        # Not a genuine failure (all green, or all skipped/inconclusive) → no signal.
        if failed <= 0:
            return None

        # Cold-start guard: require a prior PASS so a never-trusted model does not
        # fire a BLOCKER every run.
        prior = await get_runs(
            db, model_id=summary.model_id, dataset=_DATASET, limit=_PRIOR_SCAN_LIMIT,
        )
        prior_pass = any(
            r.get("id") != summary.run_id
            and _is_pass(int(r.get("passed_cases") or 0), int(r.get("failed_cases") or 0))
            for r in prior
        )
        if not prior_pass:
            logger.warning(
                "gauntlet[%s]: FAIL with no prior PASS — recorded, not a regression "
                "(cold start / never trusted)", summary.model_id,
            )
            return None

        from genesis.db.crud import ego as ego_crud

        pid = _proposal_id(summary.model_id, summary.run_id)
        # Idempotency: the proposal's existence marks this (model, run) handled.
        try:
            if await ego_crud.get_proposal(db, pid):
                return None
        except Exception:
            logger.warning(
                "gauntlet regression: existence check failed for %s",
                summary.model_id, exc_info=True,
            )
            return None

        failed_names = [
            r.case_id for r in summary.results if not r.passed and not r.skipped
        ]
        attempted = passed + failed
        text = (
            f"Model-roster gauntlet regression — {summary.model_id}: previously "
            f"PASSED, now FAILED {failed}/{attempted} fixtures "
            f"({', '.join(failed_names) or 'unknown'}). Gating is advisory — "
            f"{summary.model_id} remains in the roster and failover chain; "
            f"investigate before relying on it. Gauntlet run {summary.run_id}."
        )

        # 1. BLOCKER alert (best-effort; outreach dedups on source_id).
        if outreach_pipeline is not None:
            try:
                from genesis.outreach.types import OutreachCategory, OutreachRequest

                await outreach_pipeline.submit_raw(
                    text,
                    OutreachRequest(
                        category=OutreachCategory.BLOCKER,
                        topic=f"Gauntlet regression: {summary.model_id}",
                        context=text,
                        salience_score=1.0,
                        signal_type="gauntlet_regression_alert",
                        source_id=f"gauntlet_regression:{summary.model_id}:{summary.run_id}",
                    ),
                )
            except Exception:
                logger.warning(
                    "gauntlet regression alert failed for %s",
                    summary.model_id, exc_info=True,
                )

        # 2. Human-gated informational proposal (durable idempotent marker).
        try:
            await ego_crud.create_proposal(
                db,
                id=pid,
                action_type=_ACTION_TYPE,
                content=text,
                rationale=(
                    "The model-roster gauntlet flagged a PASS→FAIL regression. "
                    "Recommend-only: approving acknowledges it; no automated "
                    "remediation runs and the model stays in the roster."
                ),
                confidence=1.0,
                urgency="high",
                status="pending",
                ego_source="gauntlet",
            )
        except Exception:
            logger.warning(
                "gauntlet regression proposal creation failed for %s",
                summary.model_id, exc_info=True,
            )
            return None

        logger.info(
            "gauntlet regression surfaced for %s (run %s): %s",
            summary.model_id, summary.run_id, failed_names,
        )
        return {
            "model_id": summary.model_id,
            "run_id": summary.run_id,
            "failed": failed_names,
            "proposal_id": pid,
        }
    except Exception:
        logger.warning(
            "gauntlet regression check failed for %s",
            getattr(summary, "model_id", "?"), exc_info=True,
        )
        return None
