"""J-9 regression proposal — the no-op apply hook fired on resolution.

A ``j9_regression`` proposal is INFORMATIONAL (ActionDomain.NOTIFY_USER): J-9's
weekly aggregation detected a cognitive-subsystem quality regression and surfaced
it for the operator. It applies NOTHING. On approval the proposal is simply
marked ``executed`` so it leaves the board and the approved-proposal sweep never
tries to dispatch it as a session — belt-and-suspenders alongside the
``_NEVER_DISPATCH_ACTION_TYPES`` blocklist and the NOTIFY_USER domain gate. On
rejection / any other status: no-op.

Like the other recommend-only handlers (``goal_status_change``,
``cell_promotion``, ``cognitive_variant``), it is called identically from every
proposal-resolution entry point so behaviour is path-independent.
"""

from __future__ import annotations

import logging

import aiosqlite

from genesis.db.crud import ego as ego_crud

logger = logging.getLogger(__name__)

J9_REGRESSION_ACTION_TYPE = "j9_regression"


async def handle_j9_regression_resolution(
    db: aiosqlite.Connection,
    proposal: dict,
    status: object,
) -> bool:
    """Mark an approved ``j9_regression`` proposal ``executed`` (no side-effect).

    No-op unless *proposal* is a ``j9_regression`` proposal. On approval, mark it
    executed so it does not linger as 'approved' (which would clutter the board
    AND let the approved-sweep try to dispatch it as a session). On any other
    status: no-op — declining an informational notice changes nothing.

    *status* may be a ``ProposalStatus`` enum or a plain string.
    Returns True iff the proposal was marked executed.
    """
    if proposal.get("action_type") != J9_REGRESSION_ACTION_TYPE:
        return False

    status_str = getattr(status, "value", status)
    if status_str != "approved":
        return False

    # execute_proposal transitions approved → executed (it no-ops unless the
    # proposal is currently 'approved' — which it is here, the resolution flow
    # set it before firing this hook). Log rather than swallow on failure.
    try:
        return await ego_crud.execute_proposal(
            db,
            proposal["id"],
            status="executed",
            user_response="j9_regression acknowledged",
        )
    except Exception:
        logger.warning(
            "j9_regression: execute_proposal failed for %s",
            proposal.get("id"), exc_info=True,
        )
        return False
