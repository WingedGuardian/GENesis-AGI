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

# A proposal with NULL ego_source routes its follow-through to the GENESIS ego,
# by the locked B2b design decision — deliberately NOT the user ego. This is an
# ONGOING branch, not a legacy artifact: ego_source is a bare
# `ALTER TABLE ego_proposals ADD COLUMN ego_source TEXT` (db/schema/_migrations.py,
# no DEFAULT, no backfill), so proposals created without it are NULL by design
# (see tests/ego/test_realist.py::test_ego_source_null_by_default). Routing this
# operational bookkeeping onto the user ego's board would be a behavior change,
# not a fix.
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
    parked: bool = False,
) -> str | None:
    """Create a follow-through intention for a terminal dispatch.

    Returns the intention id, or None when skipped: a rate/quota-PARKED
    dispatch (not terminal — the resume fires the real follow-through), an
    unknown proposal, or an active follow-through for this proposal already
    existing (dedup so a re-dispatched proposal never piles rows). Caller commits.
    """
    if parked:
        # A rate/quota-parked dispatch is NOT terminal — creating a
        # follow-through now would post a premature "failed" review. KNOWN GAP
        # (follow-up 837f8b63): the resume does NOT currently re-fire it — the
        # resume rewrites caller_context to `rate_limit_resume:<park_id>`,
        # severing the `ego_proposal:` linkage the on_end hook gates on — so the
        # parked class is uncovered until that reconnection lands.
        # behavioral-lint: ignore no-hide-problems
        logger.info(
            "Dispatch follow-through withheld — proposal %s parked for resume",
            proposal_id[:8],
        )
        return None
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

    # Derive failure from the proposal's AUTHORITATIVE status too. A dispatch
    # whose SESSION completed but whose deliverables failed post-dispatch
    # verification is marked failed on the PROPOSAL
    # (mark_proposal_verification_failed) — invisible to the caller's
    # session-derived `failed` flag. Either signal means failed.
    failed = failed or (proposal.get("status") or "").lower() == "failed"

    prop_summary = (proposal.get("content") or "").replace("\n", " ").strip()[:80]
    # The raw session OUTCOME text is intentionally NOT embedded in this content.
    # This content lands in the owning ego's mandatory-review block — an
    # authoritative, privileged context — and a research/interact dispatch's
    # output is external_untrusted (it can echo web/browser content). Embedding
    # it here would launder indirect prompt injection into a trusted instruction.
    # status + the ego's own (first-party) proposal summary anchor the review;
    # the ego reads the full outcome via the execution_outcome observation + the
    # recallable dispatch memory.
    # Status is authoritative EXCEPT the one misleading case: a session that
    # reported "completed" but whose deliverables failed post-dispatch
    # verification (failed=True + status="completed") — a bare [completed] would
    # misreport it. Only override THAT case; keep every genuine terminal status
    # (timeout/error/cancelled) intact, since the reviewing ego uses the specific
    # outcome to choose step-2 vs retry vs close.
    _state = "failed" if (failed and (status or "").lower() == "completed") else status
    content = (
        f"Review dispatch outcome for proposal {proposal_id[:8]} "
        f"[{_state}]: {prop_summary}"
        ". Decide follow-through: step-2, retry, or close."
    )
    if outcome:
        # Preview only in the (non-privileged) application log — never the
        # ego-facing content above.
        logger.debug(
            "Follow-through for %s — outcome preview: %s",
            proposal_id[:8],
            outcome.replace("\n", " ").strip()[:80],
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
