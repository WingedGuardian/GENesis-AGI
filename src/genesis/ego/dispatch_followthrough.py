"""Dispatch follow-through — force ego review of every terminal dispatch.

B2b of the activation-loop program: a dispatched proposal that finishes
(success OR failure) previously produced only an observation + memory that
nothing was forced to read. This module creates a system-origin
``ego_intentions`` row for the OWNING ego, so its next cycle's
mandatory-review block makes it judge the outcome: step-2, retry, or close.

Called from the ``_ego_dispatch_on_end`` hook (``runtime/init/ego.py``) —
kept here as a module-level function so it is unit-testable outside the
runtime closure. Caller owns the commit.
"""

from __future__ import annotations

import logging

import aiosqlite

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import ego_intentions

logger = logging.getLogger(__name__)

_FALLBACK_EGO_SOURCE = "genesis_ego_cycle"

_TRIGGER = "Next ego cycle — review this dispatch outcome and decide follow-through."


async def record_dispatch_followthrough(
    db: aiosqlite.Connection,
    *,
    proposal_id: str,
    session_id: str,
    status: str,
    outcome: str = "",
    failed: bool = False,
) -> str | None:
    """Create a follow-through intention for a terminal dispatch.

    Returns the intention id, or None when skipped (unknown proposal, or an
    active follow-through for this proposal already exists — dedup so a
    re-dispatched proposal never piles rows). Caller commits.
    """
    proposal = await ego_crud.get_proposal(db, proposal_id)
    if proposal is None:
        logger.warning(
            "Dispatch follow-through skipped — proposal %s not found",
            proposal_id[:8],
        )
        return None

    ego_source = proposal.get("ego_source") or _FALLBACK_EGO_SOURCE

    if await ego_intentions.has_active_for_proposal(db, ego_source, proposal_id):
        logger.info(
            "Dispatch follow-through deduped — active intention already references proposal %s",
            proposal_id[:8],
        )
        return None

    prop_summary = (proposal.get("content") or "").replace("\n", " ").strip()[:80]
    outcome_summary = (outcome or "").replace("\n", " ").strip()[:120]
    content = (
        f"Review dispatch outcome for proposal {proposal_id[:8]} "
        f"[{status}]: {prop_summary}"
        + (f" — {outcome_summary}" if outcome_summary else "")
        + ". Decide follow-through: step-2, retry, or close."
    )

    iid = await ego_intentions.create(
        db,
        content=content,
        trigger_condition=_TRIGGER,
        ego_source=ego_source,
        reasoning=(
            f"Auto-created by dispatch on_end hook (session {session_id[:8]}); "
            "every terminal dispatch gets one forced outcome review."
        ),
        priority="high" if failed else "normal",
        max_cycles=3,
        origin="system",
        proposal_id=proposal_id,
    )
    if iid:
        logger.info(
            "Dispatch follow-through intention %s created for %s (proposal %s)",
            iid,
            ego_source,
            proposal_id[:8],
        )
    return iid
