"""Shared proposal-resolution hook — one artifact set for every entry point.

A proposal can be resolved from four places: the Telegram parser
(``ProposalWorkflow.resolve_proposals``), the dashboard route
(``routes/ego.py``), the MCP tool (``ego_proposal_resolve``), and the
conversational free-text path. Before this module each ran its own subset
of side effects — only Telegram wrote a correction memory, only
Telegram/dashboard wrote the intervention journal, and NOTHING captured the
user's ruling anywhere the ego context builders render. The result: a typed
deny reason (the strongest engagement signal the user can give) evaporated,
and the ego re-litigated settled decisions.

``handle_proposal_resolution`` is the single post-resolution hook. Every
block is isolated (one failing never blocks the others or the resolution
itself), and every entry point calls it exactly once per resolved proposal,
AFTER the ``resolve_proposal`` status update succeeds.

Decision capture: a rejection WITH a reason that is not marked one-off
becomes (or reaffirms) an ``ego_directives`` row with ``kind='decision'`` —
the durable, always-rendered ruling. ``standing_rule`` lets an LLM-bearing
caller (the conversational path) pass a distilled ruling; the deterministic
fallback stores the verbatim reason.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Cap for stored decision content — rulings are constraints, not essays.
_DECISION_CONTENT_MAX = 500


def decision_prefix(proposal: dict) -> str:
    """Stable dedup prefix for a proposal's theme: ``[action_type/category]``."""
    action_type = proposal.get("action_type") or "unknown"
    category = proposal.get("action_category") or "general"
    return f"[{action_type}/{category}]"


async def handle_proposal_resolution(
    db,
    proposal: dict,
    status: str,
    *,
    reason: str | None = None,
    source: str,
    memory_store=None,
    autonomy_manager=None,
    standing_rule: str | None = None,
    one_off: bool = False,
) -> None:
    """Run every post-resolution side effect for one resolved proposal.

    Args:
        db: open aiosqlite connection.
        proposal: the proposal row dict (pre- or post-resolution).
        status: "approved" / "rejected" (final status already persisted).
        reason: the user's typed reason, if any (recorded upstream as
            ``user_response``).
        source: entry point tag for logging/journal ("telegram",
            "dashboard", "mcp", "conversation").
        memory_store: optional MemoryStore for the correction memory
            (entry points without runtime embeddings pass None — the
            DB decision row is canonical; the memory is enrichment).
        autonomy_manager: optional AutonomyManager for the earn-back hook.
        standing_rule: optional LLM-distilled ruling text; overrides the
            verbatim-reason fallback for the decision row.
        one_off: the user marked this rejection as situational — skip
            decision capture (still journals + corrects).
    """
    proposal_id = proposal.get("id", "?")

    # ── J-9 eval: proposal-resolution quality tracking ──────────────────
    try:
        from genesis.eval.j9_hooks import emit_proposal_resolved

        await emit_proposal_resolved(
            db,
            proposal_id=proposal_id,
            status=status,
            confidence=proposal.get("confidence"),
            action_type=proposal.get("action_type"),
        )
    except Exception:
        logger.warning("J-9 resolution emit failed for %s", proposal_id, exc_info=True)

    # ── Intervention journal ────────────────────────────────────────────
    try:
        from genesis.db.crud import intervention_journal as journal_crud

        await journal_crud.resolve(
            db,
            proposal_id,
            outcome_status=status,
            actual_outcome=f"{source}: user {status}" + (f": {reason}" if reason else ""),
            user_response=reason or None,
        )
    except Exception:
        logger.warning("Journal resolve failed for %s", proposal_id, exc_info=True)

    # ── Decision capture — the durable ruling ───────────────────────────
    if status == "rejected" and reason and not one_off:
        try:
            await _capture_decision(
                db,
                proposal,
                reason=reason,
                standing_rule=standing_rule,
            )
        except Exception:
            logger.warning("Decision capture failed for %s", proposal_id, exc_info=True)

    # ── Correction memory (enrichment; DB row above is canonical) ───────
    if status == "rejected" and reason and memory_store is not None:
        try:
            await _store_correction_memory(memory_store, proposal, reason)
        except Exception:
            logger.warning("Correction memory failed for %s", proposal_id, exc_info=True)

    # ── Existing action hooks (verbatim behavior, now shared) ───────────
    try:
        from genesis.ego.earnback import handle_earnback_resolution

        await handle_earnback_resolution(db, proposal, status, autonomy_manager)
    except Exception:
        logger.warning("earnback hook failed for %s", proposal_id, exc_info=True)

    try:
        from genesis.ego.goal_actions import handle_goal_status_change_resolution

        await handle_goal_status_change_resolution(db, proposal, status)
    except Exception:
        logger.warning("goal status-change hook failed for %s", proposal_id, exc_info=True)

    try:
        from genesis.ego.cell_promotion import handle_cell_promotion_resolution

        await handle_cell_promotion_resolution(db, proposal, status)
    except Exception:
        logger.warning("cell promotion hook failed for %s", proposal_id, exc_info=True)

    try:
        from genesis.ego.cognitive_variant import handle_cognitive_variant_resolution

        await handle_cognitive_variant_resolution(db, proposal, status)
    except Exception:
        logger.warning("cognitive-variant hook failed for %s", proposal_id, exc_info=True)

    try:
        from genesis.ego.j9_regression_actions import handle_j9_regression_resolution

        await handle_j9_regression_resolution(db, proposal, status)
    except Exception:
        logger.warning("j9 regression hook failed for %s", proposal_id, exc_info=True)

    try:
        from genesis.ego.gauntlet_regression_actions import (
            handle_gauntlet_regression_resolution,
        )

        await handle_gauntlet_regression_resolution(db, proposal, status)
    except Exception:
        logger.warning("gauntlet regression hook failed for %s", proposal_id, exc_info=True)


async def _capture_decision(
    db,
    proposal: dict,
    *,
    reason: str,
    standing_rule: str | None,
) -> None:
    """Create or reaffirm the ``kind='decision'`` row for this ruling."""
    from genesis.db.crud import ego as ego_crud

    prefix = decision_prefix(proposal)
    ego_target = "genesis_ego" if proposal.get("ego_source") == "genesis_ego_cycle" else "user_ego"

    existing = await ego_crud.find_active_decision(
        db,
        prefix=prefix,
        ego_target=ego_target,
    )
    if existing:
        await ego_crud.reaffirm_decision(db, existing["id"])
        logger.info(
            "Decision reaffirmed (%s, count=%d) for proposal %s",
            existing["id"],
            existing.get("reaffirm_count", 0) + 1,
            proposal.get("id", "?"),
        )
        return

    ruling = (standing_rule or reason).strip()
    snippet = (proposal.get("content") or "")[:150]
    content = f"{prefix} {ruling}"
    if standing_rule is None:
        content += f" (rejected proposal: {snippet})"
    decision_id = await ego_crud.create_decision(
        db,
        content=content[:_DECISION_CONTENT_MAX],
        ego_target=ego_target,
        source_proposal_id=proposal.get("id"),
    )
    logger.info(
        "Decision captured (%s) from proposal %s",
        decision_id,
        proposal.get("id", "?"),
    )


async def _store_correction_memory(memory_store, proposal: dict, reason: str) -> None:
    """Correction memory on rejection-with-reason (moved from proposals.py)."""
    action_type = proposal.get("action_type", "unknown")
    action_category = proposal.get("action_category", "")
    content_snippet = proposal.get("content", "")[:200]
    correction_text = (
        f"User rejected [{action_type}]: {content_snippet}. Reason: {reason}. Do not repeat."
    )
    tags = ["ego_correction"]
    if action_category:
        tags.append(action_category)
    await memory_store.store(
        content=correction_text,
        source="ego_correction",
        tags=tags,
        wing="autonomy",
        room="ego_corrections",
        source_subsystem="ego",
    )
    logger.info("Stored ego correction for rejected proposal %s", proposal.get("id", "?"))
