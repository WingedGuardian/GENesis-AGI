"""Cell promotion — the shared hook fired when a ``cell_promotion`` proposal is
resolved (WS-8 PR-D).

A ``cell_promotion`` proposal is created by the ego cadence
(``EgoCadenceManager._check_cell_promotion_opportunities``) when an email
capability cell has earned enough approved competence to be PROPOSED for
standing autonomy.  **Recommend-only:** the cell is promoted to GRANTED only on
the user's explicit approval, never autonomously — the user is the gate.

Like ``autonomy_earnback`` / ``goal_status_change``, a proposal can be resolved
from four entry points (a Telegram reply, the ``ego_proposal_resolve`` MCP tool,
and two dashboard routes), so the promote/cooldown logic lives here and every
path calls it identically.  It does ONLY the cell-promotion side-effect, leaving
each path's other behaviour untouched.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime

import aiosqlite

from genesis.autonomy.types import CellEvent, CellState
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import ego as ego_crud

logger = logging.getLogger(__name__)

CELL_PROMOTION_ACTION_TYPE = "cell_promotion"


def _parse_cell(expected_outputs: object) -> tuple[str, str, str] | None:
    """Extract the ``(domain, verb, risk_class)`` cell from a proposal's
    expected_outputs (stored as JSON ``{"cell": [domain, verb, risk]}``)."""
    data = expected_outputs
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    cell = data.get("cell")
    if not isinstance(cell, list | tuple) or len(cell) != 3:
        return None
    domain, verb, risk = (str(x) for x in cell)
    return domain, verb, risk


async def handle_cell_promotion_resolution(
    db: aiosqlite.Connection,
    proposal: dict,
    status: object,
) -> bool:
    """Apply the cell-promotion side-effect of resolving *proposal*.

    No-op unless *proposal* is a ``cell_promotion`` proposal.  On rejection:
    record a reject timestamp so the cadence applies its cooldown before
    re-proposing.  On approval: re-verify the cell is STILL promotable (a
    correction may have landed since the proposal was shown — never promote on
    stale evidence), then promote ASK→GRANTED via ``apply_event(APPROVE)`` and
    mark the proposal ``executed`` so the approved-proposal sweep never
    dispatches it as a session.

    *status* may be a ``ProposalStatus`` enum or a plain string.
    Returns True iff a promotion was applied.
    """
    if proposal.get("action_type") != CELL_PROMOTION_ACTION_TYPE:
        return False

    status_str = getattr(status, "value", status)
    cell_key = (proposal.get("action_category") or "").strip()

    if status_str == "rejected":
        if cell_key:
            with contextlib.suppress(Exception):
                await ego_crud.set_state(
                    db,
                    key=f"cell_promotion_reject:{cell_key}",
                    value=datetime.now(UTC).isoformat(),
                )
        return False

    if status_str != "approved":
        return False

    cell = _parse_cell(proposal.get("expected_outputs"))
    if cell is None:
        logger.warning(
            "cell_promotion proposal %s missing/invalid expected_outputs cell",
            proposal.get("id"),
        )
        return False

    domain, verb, risk = cell
    now = datetime.now(UTC).isoformat()

    # Staleness guard: re-fetch THIS cell immediately before promoting (not a
    # batch scan taken moments earlier — that leaves a TOCTOU window for a
    # concurrent correction).  A correction landing between the proposal and the
    # approval demotes the cell + craters its re-earn posterior, so the evidence
    # the owner approved on no longer holds — don't promote.
    current = await cg.get_cell(db, domain, verb, risk)
    still_promotable = (
        current is not None
        and current["state"] == CellState.ASK.value
        and current["successes"] >= cg.MIN_PROMOTE_N
        and cg.cell_posterior(
            current["successes"], current["corrections"],
            current["weighted_corrections"] or 0.0,
        ) >= cg.PROMOTE_THRESHOLD
    )
    if not still_promotable:
        logger.info(
            "cell_promotion for %s skipped — no longer promotable (evidence changed)",
            cell_key,
        )
        with contextlib.suppress(Exception):
            await ego_crud.execute_proposal(
                db, proposal["id"], status="executed",
                user_response="cell promotion skipped: evidence changed",
            )
        return False

    try:
        await cg.apply_event(
            db, domain=domain, verb=verb, risk_class=risk,
            event=CellEvent.APPROVE, updated_at=now,
        )
        ok = True
    except Exception:
        logger.warning(
            "cell_promotion apply_event failed for %s", cell_key, exc_info=True,
        )
        ok = False

    # Mark executed regardless so the proposal doesn't linger as 'approved'
    # (which would clutter the board AND let the sweep dispatch it as a session).
    with contextlib.suppress(Exception):
        await ego_crud.execute_proposal(
            db, proposal["id"], status="executed",
            user_response=(
                f"cell {cell_key} promoted to GRANTED" if ok
                else f"cell {cell_key} promotion failed"
            ),
        )
    if ok:
        logger.info("Cell promotion applied: %s → GRANTED (user approved)", cell_key)
    return ok
