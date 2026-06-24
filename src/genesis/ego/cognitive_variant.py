"""Cognitive-variant promotion — the resolution hook fired when a
``cognitive_variant_promotion`` proposal is resolved (Evo PR-B).

The proposal carries a reflection-prompt winner that Evo measured against the
canonical prompt (held-out validated, above the confidence floor). **Recommend-
only:** the prompt is applied to the live deep-reflection overlay ONLY on the
user's explicit approval, never autonomously — the user is the gate.

Like ``autonomy_earnback`` / ``goal_status_change`` / ``cell_promotion``, a
proposal can be resolved from four entry points (a Telegram reply, the
``ego_proposal_resolve`` MCP tool, and two dashboard routes), so the apply logic
lives here and every path calls it identically.

The write goes through ``cognitive_ledger.record_file_modification`` so it
captures a pre-image and is reversible via ``cognitive_modification_rollback``.
The proposal is marked ``executed`` UNCONDITIONALLY after an approval (even if
the write fails) so it never lingers ``approved`` — the dispatch sweep/briefs
guards are defense-in-depth; this is the primary recommend-only guarantee.
"""

from __future__ import annotations

import contextlib
import json
import logging

import aiosqlite

from genesis.db.crud import ego as ego_crud

logger = logging.getLogger(__name__)

COGNITIVE_VARIANT_ACTION_TYPE = "cognitive_variant_promotion"

# Minimum confidence to apply a promotion. Belt-and-suspenders: the promote path
# (``promote_evo_winner``) already refuses to FILE below this, but the handler
# re-checks so a sub-floor proposal that arrives by any other route is refused.
_MIN_PROMOTE_CONFIDENCE = 0.75

# The deep-reflection prompt file. Must match the name the reflection bridge
# reads — ``_prompts._DEPTH_FILES[Depth.DEEP]`` (test_overlay_target_matches_
# bridge_read guards against drift).
_OVERLAY_FILENAME = "REFLECTION_DEEP.md"


def _parse_expected_outputs(expected_outputs: object) -> dict:
    """Decode a proposal's ``expected_outputs`` (stored as JSON) to a dict.
    Returns ``{}`` on anything unparseable."""
    data = expected_outputs
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return {}
    return data if isinstance(data, dict) else {}


async def _mark_executed(db: aiosqlite.Connection, proposal: dict, response: str) -> None:
    """Mark the proposal ``executed`` (best-effort) so it never lingers
    ``approved``."""
    with contextlib.suppress(Exception):
        await ego_crud.execute_proposal(
            db, proposal["id"], status="executed", user_response=response,
        )


async def handle_cognitive_variant_resolution(
    db: aiosqlite.Connection,
    proposal: dict,
    status: object,
) -> bool:
    """Apply the reflection-prompt promotion side-effect of resolving *proposal*.

    No-op unless *proposal* is a ``cognitive_variant_promotion`` proposal.
    On rejection / any non-approved status: no-op (recommend-only — declining
    changes nothing). On approval: validate the winner prompt + confidence floor,
    write it to the live reflection overlay via the rollback-able cognitive
    ledger, and mark the proposal ``executed``.

    *status* may be a ``ProposalStatus`` enum or a plain string.
    Returns True iff the overlay was written.
    """
    if proposal.get("action_type") != COGNITIVE_VARIANT_ACTION_TYPE:
        return False

    status_str = getattr(status, "value", status)
    if status_str != "approved":
        # Recommend-only: a declined / pending / failed proposal is left as-is.
        return False

    spec = _parse_expected_outputs(proposal.get("expected_outputs"))
    full_prompt = spec.get("full_prompt")

    # Validate before any write. An approved-but-unappliable proposal is marked
    # executed (not left 'approved') so it doesn't clutter the board / get swept.
    if not isinstance(full_prompt, str) or not full_prompt.strip():
        logger.warning(
            "cognitive_variant promotion %s refused: missing/empty full_prompt",
            proposal.get("id"),
        )
        await _mark_executed(db, proposal, "promotion refused: missing prompt")
        return False

    try:
        confidence = float(proposal.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < _MIN_PROMOTE_CONFIDENCE:
        logger.warning(
            "cognitive_variant promotion %s refused: confidence %.2f < floor %.2f",
            proposal.get("id"), confidence, _MIN_PROMOTE_CONFIDENCE,
        )
        await _mark_executed(
            db, proposal,
            f"promotion refused: confidence {confidence:.2f} below floor",
        )
        return False

    # Apply: write the winner to the live deep-reflection overlay via the ledger
    # (captures a pre-image; reversible via cognitive_modification_rollback).
    from genesis.cc.reflection_bridge._prompts import _reflection_override_dir
    from genesis.learning import cognitive_ledger

    overlay_path = _reflection_override_dir() / _OVERLAY_FILENAME
    mod_id: str | None = None
    ok = False
    try:
        mod_id = await cognitive_ledger.record_file_modification(
            db,
            actor="evo_promotion",
            path=overlay_path,
            new_content=full_prompt,
            summary=f"Evo reflection-prompt promotion: {spec.get('approach', 'variant')}",
            metadata={
                "proposal_id": proposal.get("id"),
                "approach": spec.get("approach"),
                "evidence": spec.get("evidence"),
            },
        )
        ok = True
    except Exception:
        logger.warning(
            "cognitive_variant promotion %s: overlay write failed",
            proposal.get("id"), exc_info=True,
        )

    # Mark executed UNCONDITIONALLY — even if the write failed. An approved
    # proposal that stayed 'approved' could be grabbed by the dispatch sweep;
    # the blocklist + SELF_MODIFY gate guard that, but this removes the risk at
    # the source (mirrors goal_actions / cell_promotion).
    await _mark_executed(
        db, proposal,
        f"reflection prompt promoted (mod {mod_id})" if ok
        else "reflection prompt promotion failed (write error)",
    )

    if ok:
        logger.info(
            "Cognitive variant promoted: reflection overlay updated at %s "
            "(mod=%s, user approved)",
            overlay_path, mod_id,
        )
    return ok
