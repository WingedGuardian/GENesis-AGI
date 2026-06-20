"""Goal status change — the shared apply hook fired when a ``goal_status_change``
proposal is resolved.

A ``goal_status_change`` proposal is created by the ego's goal-review
post-processing (``EgoSession._surface_goal_recommendation``) when the ego
recommends *pausing* or *deprioritizing* a stale/stuck goal. **Recommend-only:**
the change is applied ONLY on the user's explicit approval, never autonomously —
the user is the gate.

Like ``autonomy_earnback``, a proposal can be resolved from four entry points —
a Telegram reply (``ProposalWorkflow.resolve_proposals``), the
``ego_proposal_resolve`` MCP tool, and two dashboard routes — so the apply
logic lives here and every path calls it identically. This is intentionally
narrow: it does ONLY the goal-status side-effect, leaving each path's other
behaviour (journal, correction memory, earn-back) untouched.
"""

from __future__ import annotations

import contextlib
import json
import logging

import aiosqlite

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import user_goals as goals_crud

logger = logging.getLogger(__name__)

GOAL_STATUS_CHANGE_ACTION_TYPE = "goal_status_change"

# The reversible transitions Genesis may *propose* (terminal achieve/abandon
# stay a pure observation — the user decides those). Validated here so a
# malformed expected_outputs can never write an arbitrary value.
_VALID_STATUS = frozenset({"paused", "active"})
_VALID_PRIORITY = frozenset({"low", "medium", "high", "critical"})


def _parse_expected_outputs(expected_outputs: object) -> dict | None:
    """Extract the ``{change, value}`` spec from a proposal's expected_outputs.

    Stored as a JSON string but may already be a dict.
    """
    data = expected_outputs
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    return data


async def handle_goal_status_change_resolution(
    db: aiosqlite.Connection,
    proposal: dict,
    status: object,
) -> bool:
    """Apply the goal-status side-effect of resolving *proposal*.

    No-op unless *proposal* is a ``goal_status_change`` proposal. On approval:
    parse ``expected_outputs`` ``{change: 'status'|'priority', value: ...}``,
    apply it to the goal via ``user_goals.update``, and mark the proposal
    ``executed`` so the approved-proposal sweep never dispatches it as a session.
    On rejection / any other status: no-op (the goal is left exactly as-is —
    recommend-only means the user declining changes nothing).

    *status* may be a ``ProposalStatus`` enum or a plain string.
    Returns True iff a change was applied.
    """
    if proposal.get("action_type") != GOAL_STATUS_CHANGE_ACTION_TYPE:
        return False

    status_str = getattr(status, "value", status)
    if status_str != "approved":
        return False

    goal_id = proposal.get("goal_id")
    spec = _parse_expected_outputs(proposal.get("expected_outputs"))
    if not goal_id or spec is None:
        logger.warning(
            "goal_status_change proposal %s missing goal_id/expected_outputs",
            proposal.get("id"),
        )
        return False

    change = spec.get("change")
    value = spec.get("value")
    if change == "status" and value in _VALID_STATUS:
        fields = {"status": value}
    elif change == "priority" and value in _VALID_PRIORITY:
        fields = {"priority": value}
    else:
        logger.warning(
            "goal_status_change proposal %s: invalid change=%r value=%r",
            proposal.get("id"), change, value,
        )
        return False

    try:
        ok = await goals_crud.update(db, goal_id, **fields)
    except Exception:
        logger.warning(
            "goal_status_change: update failed for goal %s",
            goal_id, exc_info=True,
        )
        ok = False

    # Mark executed regardless of the update result so the proposal doesn't
    # linger as 'approved' (which would clutter the board AND let the sweep
    # try to dispatch it as a session). Mirrors the earn-back hook.
    with contextlib.suppress(Exception):
        await ego_crud.execute_proposal(
            db,
            proposal["id"],
            status="executed",
            user_response=(
                f"goal {change}→{value} applied" if ok
                else f"goal {change}→{value} no-op (goal missing?)"
            ),
        )

    if ok:
        logger.info(
            "Goal status change applied: %s %s→%s (user approved)",
            goal_id[:12], change, value,
        )
    return ok
