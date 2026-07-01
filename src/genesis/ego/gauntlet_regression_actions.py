"""Gauntlet regression proposal — the no-op apply hook fired on resolution.

A ``gauntlet_regression`` proposal is INFORMATIONAL (ActionDomain.NOTIFY_USER):
the model-roster gauntlet detected that a roster model which previously PASSED now
FAILS, and surfaced it for the operator. It applies NOTHING — gating is advisory,
so a regression never auto-removes a model from selection/failover. On approval the
proposal is simply marked ``executed`` so it leaves the board and the approved-
proposal sweep never tries to dispatch it as a session — belt-and-suspenders
alongside the ``_NEVER_DISPATCH_ACTION_TYPES`` blocklist and the NOTIFY_USER gate.
On rejection / any other status: no-op.

Mirrors ``j9_regression_actions`` (the sibling recommend-only handler); it is
called identically from every proposal-resolution entry point so behaviour is
path-independent.
"""

from __future__ import annotations

import logging

import aiosqlite

from genesis.db.crud import ego as ego_crud

logger = logging.getLogger(__name__)

GAUNTLET_REGRESSION_ACTION_TYPE = "gauntlet_regression"


async def handle_gauntlet_regression_resolution(
    db: aiosqlite.Connection,
    proposal: dict,
    status: object,
) -> bool:
    """Mark an approved ``gauntlet_regression`` proposal ``executed`` (no side-effect).

    No-op unless *proposal* is a ``gauntlet_regression`` proposal. On approval,
    mark it executed so it does not linger as 'approved'. On any other status:
    no-op. *status* may be a ``ProposalStatus`` enum or a plain string. Returns
    True iff the proposal was marked executed.
    """
    if proposal.get("action_type") != GAUNTLET_REGRESSION_ACTION_TYPE:
        return False

    status_str = getattr(status, "value", status)
    if status_str != "approved":
        return False

    try:
        return await ego_crud.execute_proposal(
            db,
            proposal["id"],
            status="executed",
            user_response="gauntlet_regression acknowledged",
        )
    except Exception:
        logger.warning(
            "gauntlet_regression: execute_proposal failed for %s",
            proposal.get("id"), exc_info=True,
        )
        return False
