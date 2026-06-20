"""Autonomy earn-back — the shared promote hook fired when an earn-back proposal
is resolved.

An ``autonomy_earnback`` proposal is created by the ego cadence when a demoted
autonomy category's Bayesian evidence again supports its earned level (see
``EgoCadenceManager._check_earnback_opportunities``). Promotion is gated on the
user's explicit approval.

A proposal can be resolved from four entry points — a Telegram reply
(``ProposalWorkflow.resolve_proposals``), the ``ego_proposal_resolve`` MCP tool,
and two dashboard routes — so the promote/cooldown logic lives here and every
path calls it identically. This is intentionally narrow: it does ONLY the
earn-back side-effect, leaving each path's other behaviour (J-9, journal,
correction memory) untouched.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import autonomy as autonomy_crud
from genesis.db.crud import ego as ego_crud

logger = logging.getLogger(__name__)

EARNBACK_ACTION_TYPE = "autonomy_earnback"


def _parse_target_level(expected_outputs: object) -> int | None:
    """Extract the integer ``target_level`` from a proposal's expected_outputs.

    ``expected_outputs`` is stored as a JSON string but may already be a dict.
    """
    data = expected_outputs
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    val = data.get("target_level")
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _regression_after(last_regression: str | None, created: str | None) -> bool:
    """True if a regression timestamp strictly postdates the proposal creation.

    Parses both to timezone-aware datetimes (rather than comparing ISO strings
    lexically, which is brittle if formats ever diverge). On any parse failure
    returns False — a freshness check that can't be evaluated must not block a
    user-approved, evidence-gated promotion.
    """
    if not last_regression or not created:
        return False
    try:
        lr = datetime.fromisoformat(last_regression)
        cr = datetime.fromisoformat(created)
    except (ValueError, TypeError):
        return False
    if lr.tzinfo is None:
        lr = lr.replace(tzinfo=UTC)
    if cr.tzinfo is None:
        cr = cr.replace(tzinfo=UTC)
    return lr > cr


async def handle_earnback_resolution(
    db: aiosqlite.Connection,
    proposal: dict,
    status: object,
    autonomy_manager: object | None,
) -> bool:
    """Apply the earn-back side-effect of resolving *proposal*.

    No-op unless *proposal* is an ``autonomy_earnback`` proposal. On approval:
    re-read the live autonomy state, skip the promote if a regression landed
    after the proposal was created (the evidence the user approved on no longer
    holds), otherwise call ``manager.promote()`` and mark the proposal
    ``executed`` so the approved-proposal sweep never dispatches it as a session.
    On rejection: record a reject timestamp so the cadence applies its cooldown
    before re-proposing.

    *status* may be a ``ProposalStatus`` enum or a plain string.
    Returns True iff a promotion was applied.
    """
    if proposal.get("action_type") != EARNBACK_ACTION_TYPE:
        return False

    status_str = getattr(status, "value", status)
    category = (proposal.get("action_category") or "").strip()

    if status_str == "rejected":
        if category:
            with contextlib.suppress(Exception):
                await ego_crud.set_state(
                    db,
                    key=f"earnback_reject:{category}",
                    value=datetime.now(UTC).isoformat(),
                )
        return False

    if status_str != "approved":
        return False

    if autonomy_manager is None:
        logger.warning(
            "earnback proposal %s approved but no autonomy_manager available",
            proposal.get("id"),
        )
        return False

    target = _parse_target_level(proposal.get("expected_outputs"))
    if not category or target is None:
        logger.warning(
            "earnback proposal %s missing category/target_level (cat=%r target=%r)",
            proposal.get("id"), category, target,
        )
        return False

    # Staleness guard: if a regression landed after the proposal was created, the
    # evidence the user approved on no longer holds — mark executed, don't promote.
    try:
        row = await autonomy_crud.get_by_category(db, category)
    except Exception:
        logger.warning(
            "earnback: could not re-read autonomy state for %s", category, exc_info=True,
        )
        row = None
    if row is not None and _regression_after(
        row.get("last_regression_at"), proposal.get("created_at"),
    ):
        logger.info(
            "earnback for %s skipped — regression at %s postdates proposal %s",
            category, row.get("last_regression_at"), proposal.get("created_at"),
        )
        with contextlib.suppress(Exception):
            await ego_crud.execute_proposal(
                db, proposal["id"], status="executed",
                user_response="earnback skipped: conditions changed",
            )
        return False

    ok = await autonomy_manager.promote(
        category, target, reason="user_approved_earnback",
    )
    if ok:
        with contextlib.suppress(Exception):
            await ego_crud.execute_proposal(
                db, proposal["id"], status="executed",
                user_response=f"earnback applied: restored to L{target}",
            )
        logger.info(
            "Autonomy earn-back applied: %s restored to L%d (user approved)",
            category, target,
        )
    else:
        # promote() declined (already at/above target, or category missing).
        # Mark executed anyway so the proposal doesn't linger as 'approved' —
        # which would clutter the dashboard AND block the cadence from
        # re-proposing (its pending-check only sees 'pending' rows).
        logger.warning("earnback promote returned False for %s -> L%s", category, target)
        with contextlib.suppress(Exception):
            await ego_crud.execute_proposal(
                db, proposal["id"], status="executed",
                user_response=f"earnback no-op: promote to L{target} declined",
            )
    return ok
