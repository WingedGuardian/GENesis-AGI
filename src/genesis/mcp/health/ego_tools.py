"""MCP tools for ego management — focus reset, directives, goal CRUD, progress.

Allows foreground CC sessions to interact with the ego system:
- Reset focus (break holdback loops)
- Create directives (flag things as important for the ego)
- Manage goals (create, list, update, achieve, abandon, add progress notes)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Validation sets matching DB CHECK constraints
_VALID_DIRECTIVE_PRIORITIES = frozenset({"low", "normal", "high", "critical"})
_VALID_EGO_TARGETS = frozenset({"user_ego", "genesis_ego"})
_VALID_GOAL_CATEGORIES = frozenset({"career", "project", "learning", "relationship", "financial", "other"})
_VALID_GOAL_PRIORITIES = frozenset({"low", "medium", "high", "critical"})
_VALID_GOAL_TYPES = frozenset({"milestone", "continuous"})


def _get_db_path():
    """Late-import DB path."""
    from genesis.env import genesis_db_path

    return genesis_db_path()


async def _impl_ego_focus_reset(
    new_focus: str | None = None,
) -> dict:
    """Reset the ego's focus_summary in ego_state.

    If new_focus is provided, sets it as the new focus.
    Otherwise clears to a neutral default.
    """
    from genesis.db.connection import get_raw_db
    from genesis.db.crud import ego as ego_crud

    default_focus = "general system awareness"
    focus_to_set = new_focus.strip() if new_focus else default_focus

    # Note: focus_summary is now system-computed each ego cycle
    # (computed_focus.py). Manual resets are temporary one-cycle
    # overrides — the next ego cycle will recompute from DB state.

    results = {}
    db_path = _get_db_path()

    async with get_raw_db(db_path) as db:
        for key in ("ego_focus_summary", "genesis_ego_focus_summary"):
            old_val = await ego_crud.get_state(db, key)
            if old_val is not None:
                await ego_crud.set_state(db, key=key, value=focus_to_set)
                results[key] = {"old": old_val, "new": focus_to_set}
        await db.commit()

        try:
            from genesis.memory.essential_knowledge import generate_and_write

            path = await generate_and_write(db)
            results["essential_knowledge"] = f"regenerated at {path}"
        except Exception:
            logger.warning(
                "Failed to regenerate essential_knowledge", exc_info=True,
            )
            results["essential_knowledge"] = "regeneration failed (non-fatal)"

    return {
        "status": "reset",
        "focus_set_to": focus_to_set,
        "details": results,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@mcp.tool()
async def ego_focus_reset(
    new_focus: str = "",
) -> dict:
    """Reset the ego's focus summary, breaking any self-reinforcing holdback loop.

    Use when the ego has put itself into a 'holding back' or dormant state
    despite not being instructed to do so. Also useful when a foreground
    session user says something like 'snap out of it' or 'why aren't you
    proposing anything?'

    If new_focus is provided, sets it as the new direction (must describe
    a TOPIC, not a behavioral state). Otherwise resets to 'general system
    awareness'. Regenerates essential_knowledge.md after the reset.
    """
    return await _impl_ego_focus_reset(new_focus or None)


@mcp.tool()
async def ego_directive(
    content: str,
    priority: str = "normal",
    ego_target: str = "user_ego",
) -> dict:
    """Create a directive for the ego — flags something as important for
    the ego's next thinking cycle.

    Directives are context, not commands. The ego considers them as input
    to its reasoning but decides what to propose. Use this when you want
    the ego to pay attention to something specific.

    Args:
        content: What you want the ego to consider (e.g., "Retry the
            Medium article publish — VNC bypass is fixed now")
        priority: low, normal, high, or critical
        ego_target: user_ego (default) or genesis_ego
    """
    if priority not in _VALID_DIRECTIVE_PRIORITIES:
        return {"status": "error", "reason": f"Invalid priority: {priority!r}. Must be one of: {sorted(_VALID_DIRECTIVE_PRIORITIES)}"}
    if ego_target not in _VALID_EGO_TARGETS:
        return {"status": "error", "reason": f"Invalid ego_target: {ego_target!r}. Must be one of: {sorted(_VALID_EGO_TARGETS)}"}

    from genesis.db.connection import get_raw_db
    from genesis.db.crud import ego as ego_crud

    db_path = _get_db_path()
    async with get_raw_db(db_path) as db:
        directive_id = await ego_crud.create_directive(
            db,
            content=content,
            priority=priority,
            ego_target=ego_target,
            source="user",
        )

    return {
        "status": "created",
        "directive_id": directive_id,
        "content": content[:100],
        "priority": priority,
        "ego_target": ego_target,
        "note": "The ego will see this in its next thinking cycle.",
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def _impl_ego_goal_create(
    title: str,
    category: str = "project",
    priority: str = "medium",
    description: str = "",
    timeline: str = "",
    parent_goal_id: str = "",
    goal_type: str = "milestone",
    cadence_days: int = 0,
) -> dict:
    """Create a new user goal for the ego system.

    Goals are visible to the ego in its thinking cycles. The ego considers
    goals when deciding what to propose. Use parent_goal_id to create
    subgoals under an existing goal.

    Args:
        title: Goal title (concise, actionable)
        category: career, project, learning, relationship, financial, other
        priority: low, medium, high, or critical
        description: Detailed description of the goal
        timeline: Expected timeline (e.g., "Q2 2026")
        parent_goal_id: ID of the parent goal (for subgoals)
        goal_type: milestone (achievable) or continuous (ongoing)
        cadence_days: Review frequency in days (0 = use global default)
    Provenance: every goal created through this tool is stamped
    origin='user' in code. The MCP surface deliberately does NOT accept an
    origin argument — 'genesis_ego' (which unlocks additive autonomous
    pause/deprioritize) may only ever be stamped by a trusted Genesis-ego
    code path (PR-3), never by caller input (Codex P1, PR #1086).
    """
    if category not in _VALID_GOAL_CATEGORIES:
        return {"status": "error", "reason": f"Invalid category: {category!r}. Must be one of: {sorted(_VALID_GOAL_CATEGORIES)}"}
    if priority not in _VALID_GOAL_PRIORITIES:
        return {"status": "error", "reason": f"Invalid priority: {priority!r}. Must be one of: {sorted(_VALID_GOAL_PRIORITIES)}"}
    if goal_type not in _VALID_GOAL_TYPES:
        return {"status": "error", "reason": f"Invalid goal_type: {goal_type!r}. Must be 'milestone' or 'continuous'"}

    from genesis.db.connection import get_raw_db
    from genesis.db.crud import user_goals

    db_path = _get_db_path()
    async with get_raw_db(db_path) as db:

        # Validate parent exists if specified
        if parent_goal_id:
            parent = await user_goals.get_by_id(db, parent_goal_id)
            if not parent:
                return {"status": "error", "reason": f"Parent goal {parent_goal_id!r} not found"}

        goal_id = await user_goals.create(
            db,
            title=title,
            category=category,
            priority=priority,
            description=description,
            timeline=timeline or None,
            parent_goal_id=parent_goal_id or None,
            goal_type=goal_type,
            cadence_days=cadence_days if cadence_days > 0 else None,
            confidence=0.9,
            # NEVER caller-controlled: the tool surface has no origin arg —
            # genesis_ego provenance requires a trusted code path (see
            # docstring). This hardcode IS the security property.
            origin="user",
        )

    return {
        "status": "created",
        "goal_id": goal_id,
        "title": title,
        "category": category,
        "priority": priority,
        "parent_goal_id": parent_goal_id or None,
        "goal_type": goal_type,
        "origin": "user",
    }


@mcp.tool()
async def ego_goal_create(
    title: str,
    category: str = "project",
    priority: str = "medium",
    description: str = "",
    timeline: str = "",
    parent_goal_id: str = "",
    goal_type: str = "milestone",
    cadence_days: int = 0,
) -> dict:
    """Create a new user goal for the ego system.

    Goals are visible to the ego in its thinking cycles. The ego considers
    goals when deciding what to propose. Use parent_goal_id to create
    subgoals under an existing goal.

    Args:
        title: Goal title (concise, actionable)
        category: career, project, learning, relationship, financial, other
        priority: low, medium, high, or critical
        description: Detailed description of the goal
        timeline: Expected timeline (e.g., "Q2 2026")
        parent_goal_id: ID of the parent goal (for subgoals)
        goal_type: milestone (achievable) or continuous (ongoing)
        cadence_days: Review frequency in days (0 = use global default)

    All goals created here are stamped origin='user' (a user directive).
    There is deliberately no origin argument — ego-owned provenance
    ('genesis_ego') can only be stamped by trusted Genesis-ego code, never
    caller input.
    """
    return await _impl_ego_goal_create(
        title=title, category=category, priority=priority,
        description=description, timeline=timeline,
        parent_goal_id=parent_goal_id, goal_type=goal_type,
        cadence_days=cadence_days,
    )


@mcp.tool()
async def ego_goal_list() -> dict:
    """List all active user goals in the ego system."""
    from genesis.db.connection import get_raw_db
    from genesis.db.crud import user_goals

    db_path = _get_db_path()
    async with get_raw_db(db_path) as db:
        goals = await user_goals.list_active(db, limit=20)

    return {
        "status": "ok",
        "count": len(goals),
        "goals": [
            {
                "id": g["id"],
                "title": g["title"][:100],
                "category": g.get("category", ""),
                "priority": g.get("priority", "medium"),
                "status": g.get("status", "active"),
            }
            for g in goals
        ],
    }


@mcp.tool()
async def ego_goal_update(
    goal_id: str,
    title: str = "",
    category: str = "",
    priority: str = "",
    description: str = "",
    timeline: str = "",
    status: str = "",
    goal_type: str = "",
    cadence_days: int = 0,
) -> dict:
    """Update an existing user goal, or mark it as achieved/abandoned.

    Pass only the fields you want to change. Empty strings / zero are ignored.

    Args:
        goal_id: The goal ID to update
        title: New title (optional)
        category: New category (optional)
        priority: New priority (optional)
        description: New description (optional)
        timeline: New timeline (optional)
        status: Set to 'achieved' or 'abandoned' to close the goal (optional)
        goal_type: milestone or continuous (optional)
        cadence_days: Review cadence in days (>0 to set, <0 to clear to global default, 0 = no change)
    """
    if category and category not in _VALID_GOAL_CATEGORIES:
        return {"status": "error", "reason": f"Invalid category: {category!r}. Must be one of: {sorted(_VALID_GOAL_CATEGORIES)}"}
    if priority and priority not in _VALID_GOAL_PRIORITIES:
        return {"status": "error", "reason": f"Invalid priority: {priority!r}. Must be one of: {sorted(_VALID_GOAL_PRIORITIES)}"}
    if status and status not in ("achieved", "abandoned"):
        return {"status": "error", "reason": f"Invalid status: {status!r}. Must be 'achieved' or 'abandoned'"}
    if goal_type and goal_type not in _VALID_GOAL_TYPES:
        return {"status": "error", "reason": f"Invalid goal_type: {goal_type!r}. Must be 'milestone' or 'continuous'"}

    from genesis.db.connection import get_raw_db
    from genesis.db.crud import user_goals

    db_path = _get_db_path()
    async with get_raw_db(db_path) as db:

        if status == "achieved":
            await user_goals.mark_achieved(db, goal_id)

            # Completion cascade: check if all siblings are achieved
            cascade_info = None
            try:
                cascade = await user_goals.check_completion_cascade(
                    db, goal_id,
                )
                if cascade:
                    import uuid

                    from genesis.db.crud import observations as obs_crud

                    await obs_crud.create(
                        db,
                        id=str(uuid.uuid4()),
                        source="goal_cascade",
                        type="goal_recommendation",
                        content=(
                            f"All subgoals of '{cascade['parent_title']}' "
                            f"are now achieved. Consider marking the parent "
                            f"goal complete (id={cascade['parent_id']})."
                        ),
                        priority="medium",
                        category="goal_review",
                        created_at=datetime.now(UTC).isoformat(),
                    )
                    await user_goals.add_progress_note(
                        db,
                        cascade["parent_id"],
                        "All subgoals achieved — cascade recommendation created",
                    )
                    cascade_info = cascade
                    logger.info(
                        "Goal cascade: all children of %s achieved",
                        cascade["parent_id"][:12],
                    )
            except Exception:
                logger.warning(
                    "Goal cascade check failed for %s", goal_id,
                    exc_info=True,
                )

            result: dict = {"status": "achieved", "goal_id": goal_id}
            if cascade_info:
                result["cascade"] = cascade_info
            return result

        if status == "abandoned":
            await user_goals.mark_abandoned(db, goal_id)
            return {"status": "abandoned", "goal_id": goal_id}

        fields: dict = {}
        if title:
            fields["title"] = title
        if description:
            fields["description"] = description
        if category:
            fields["category"] = category
        if priority:
            fields["priority"] = priority
        if timeline:
            fields["timeline"] = timeline
        if goal_type:
            fields["goal_type"] = goal_type
        if cadence_days > 0:
            fields["cadence_days"] = cadence_days
        elif cadence_days < 0:
            # Negative = explicit clear, revert to global default
            fields["cadence_days"] = None
        if not fields:
            return {"status": "error", "reason": "no fields to update"}

        await user_goals.update(db, goal_id, **fields)
        return {"status": "updated", "goal_id": goal_id, "fields": list(fields.keys())}


@mcp.tool()
async def ego_goal_progress(
    goal_id: str,
    note: str,
) -> dict:
    """Add a progress note to a user goal.

    Progress notes are visible to the ego in its thinking cycles.
    Use this to record incremental progress, blockers, or status
    changes on a goal.

    Args:
        goal_id: The goal ID to add a note to
        note: The progress note text (will be timestamped automatically)
    """
    if not note.strip():
        return {"status": "error", "reason": "Note cannot be empty"}

    from genesis.db.connection import get_raw_db
    from genesis.db.crud import user_goals

    db_path = _get_db_path()
    async with get_raw_db(db_path) as db:
        goal = await user_goals.get_by_id(db, goal_id)
        if not goal:
            return {"status": "error", "reason": f"Goal {goal_id} not found"}
        updated = await user_goals.add_progress_note(db, goal_id, note.strip())
        if not updated:
            return {"status": "error", "reason": "Failed to add note"}

    return {
        "status": "ok",
        "goal_id": goal_id,
        "goal_title": goal["title"][:80],
        "note": note.strip()[:120],
    }


async def _resolve_one_proposal(
    db,
    prop: dict,
    status: str,
    reason: str,
) -> str:
    """Resolve a single proposal (given its row dict) and run every
    post-resolution hook.

    Shared by both the by-id and by-number paths of ``ego_proposal_resolve``
    so neither path can silently skip a hook. Returns a short result string
    for the caller's details map.
    """
    from genesis.db.crud import ego as ego_crud

    # Re-validate withdrawn proposals → directive so the ego reconsiders.
    if prop.get("status") == "withdrawn":
        if status == "approved":
            directive_id = await ego_crud.create_directive(
                db,
                content=(
                    f"User approved withdrawn proposal: "
                    f"{(prop.get('content') or '')[:200]}. "
                    f"Re-propose this or explain why it's no longer valid."
                ),
                priority="high",
                ego_target="user_ego",
                source="user",
            )
            return f"withdrawn → directive ({directive_id})"
        return "already withdrawn"

    if prop.get("status") != "pending":
        return f"already {prop.get('status')}"

    updated = await ego_crud.resolve_proposal(
        db,
        prop["id"],
        status=status,
        user_response=reason or None,
    )
    if not updated:
        return "not updated"

    # Post-resolution hooks — best-effort, each isolated so one failure never
    # blocks the others or the resolution itself.
    # Autonomy earn-back: promote on approval / cooldown on reject.
    try:
        from genesis.ego.earnback import handle_earnback_resolution
        from genesis.runtime import GenesisRuntime

        _rt = GenesisRuntime.instance()
        await handle_earnback_resolution(
            db, prop, status, getattr(_rt, "_autonomy_manager", None),
        )
    except Exception:
        logger.debug("earnback resolution hook failed", exc_info=True)
    # Goal status change: apply pause/deprioritize on approval.
    try:
        from genesis.ego.goal_actions import (
            handle_goal_status_change_resolution,
        )

        await handle_goal_status_change_resolution(db, prop, status)
    except Exception:
        logger.debug("goal status-change hook failed", exc_info=True)
    # Cell promotion (WS-8 PR-D).
    try:
        from genesis.ego.cell_promotion import (
            handle_cell_promotion_resolution,
        )

        await handle_cell_promotion_resolution(db, prop, status)
    except Exception:
        logger.debug("cell promotion hook failed", exc_info=True)
    # Cognitive variant promotion (Evo PR-B).
    try:
        from genesis.ego.cognitive_variant import (
            handle_cognitive_variant_resolution,
        )

        await handle_cognitive_variant_resolution(db, prop, status)
    except Exception:
        logger.debug("cognitive-variant hook failed", exc_info=True)
    # J-9 regression (informational): mark executed on approval.
    try:
        from genesis.ego.j9_regression_actions import (
            handle_j9_regression_resolution,
        )

        await handle_j9_regression_resolution(db, prop, status)
    except Exception:
        logger.debug("j9 regression hook failed", exc_info=True)
    # Gauntlet regression (informational): mark executed on approval.
    try:
        from genesis.ego.gauntlet_regression_actions import (
            handle_gauntlet_regression_resolution,
        )

        await handle_gauntlet_regression_resolution(db, prop, status)
    except Exception:
        logger.debug("gauntlet regression hook failed", exc_info=True)

    return status


@mcp.tool()
async def ego_proposal_resolve(
    action: str,
    proposal_numbers: str = "all",
    reason: str = "",
    proposal_ids: str = "",
) -> dict:
    """Resolve pending ego proposals from a conversation.

    Use when the user expresses approval or rejection of proposals in natural
    language. This is the conversational alternative to the automated parser.

    Two ways to target proposals:

    - ``proposal_ids`` (PREFERRED, precise): comma-separated proposal IDs (the
      ``ID:`` shown on the proposal board). Resolves exactly those proposals
      regardless of which batch/digest they belong to — the only safe way to
      act on a specific or older proposal. Use this whenever the board shows an
      ID.
    - ``proposal_numbers`` (positional): comma-separated 1-based numbers, or
      "all". These index the SAME pending board the user sees (all pending
      proposals, newest first) — NOT a single batch. Position 1 is the first
      item on that board.

    If ``proposal_ids`` is provided it takes precedence and ``proposal_numbers``
    is ignored.

    Args:
        action: "approve" or "reject"
        proposal_numbers: 1-based positions on the pending board, or "all".
        reason: Optional reason (recorded as user_response; used for rejections).
        proposal_ids: Comma-separated proposal IDs for precise,
            batch-independent targeting.
    """
    if action not in ("approve", "reject"):
        return {
            "status": "error",
            "reason": f"action must be 'approve' or 'reject', got {action!r}",
        }

    from genesis.db.connection import get_raw_db
    from genesis.db.crud import ego as ego_crud

    status = "approved" if action == "approve" else "rejected"
    db_path = _get_db_path()
    results: dict[str, str] = {}
    previews: dict[str, str] = {}

    async with get_raw_db(db_path) as db:

        # ── By-ID path (precise, batch-independent) ──────────────────────
        if proposal_ids.strip():
            ids = [s.strip() for s in proposal_ids.split(",") if s.strip()]
            for pid in ids:
                prop = await ego_crud.get_proposal(db, pid)
                if not prop:
                    results[pid] = "not found"
                    continue
                previews[pid] = (prop.get("content") or "")[:100]
                results[pid] = await _resolve_one_proposal(
                    db, prop, status, reason,
                )
            resolved = sum(
                1 for v in results.values() if v in ("approved", "rejected")
            )
            return {
                "status": "ok",
                "action": action,
                "mode": "by_id",
                "resolved": resolved,
                "details": results,
                "resolved_preview": previews,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        # ── By-number path (indexes the pending board the user sees) ─────
        # Same query the proposal board is built from (conversation.py): all
        # pending proposals, newest first, capped at 10 — so a position here
        # matches the number shown to the user, not a position within one
        # batch (the source of prior wrong-target resolutions).
        # Scope to the user ego (with the pre-migration NULL fallback),
        # exactly like the Telegram parser and UserEgoContextBuilder:
        # Genesis-ego proposals are a separate surface and must not be
        # resolved from the user proposal board. proposal_ids stays
        # unscoped for precise cross-ego access.
        board = await ego_crud.list_proposals(
            db, status="pending", limit=10, ego_source="user_ego_cycle",
        )
        if not board:
            board = await ego_crud.list_proposals(db, status="pending", limit=10)

        if not board:
            # No pending — check for recently withdrawn (re-validate path).
            recent = await ego_crud.list_proposals(db, status="withdrawn", limit=5)
            if recent:
                top = recent[0]
                directive_id = await ego_crud.create_directive(
                    db,
                    content=(
                        f"User tried to {action} but all proposals were withdrawn. "
                        f"Most recent: {(top.get('content') or '')[:200]}. "
                        f"Re-propose if still valid."
                    ),
                    priority="high",
                    ego_target="user_ego",
                    source="user",
                )
                return {
                    "status": "no_pending",
                    "note": "All proposals were withdrawn. Created directive for ego to reconsider.",
                    "directive_id": directive_id,
                }
            return {"status": "error", "reason": "No pending proposals found"}

        # Determine which board positions to resolve.
        if proposal_numbers.strip().lower() == "all":
            indices = list(range(1, len(board) + 1))
        else:
            try:
                indices = [
                    int(n.strip())
                    for n in proposal_numbers.split(",")
                    if n.strip()
                ]
            except ValueError:
                return {
                    "status": "error",
                    "reason": f"Invalid numbers: {proposal_numbers!r}",
                }

        # Dedupe positions (preserving order) so a repeated number can't
        # double-run the post-resolution hooks.
        indices = list(dict.fromkeys(indices))

        for idx in indices:
            if idx < 1 or idx > len(board):
                results[f"#{idx}"] = "out of range"
                continue
            prop = board[idx - 1]
            previews[prop["id"]] = f"#{idx}: " + (prop.get("content") or "")[:90]
            results[prop["id"]] = await _resolve_one_proposal(
                db, prop, status, reason,
            )

        resolved = sum(
            1 for v in results.values() if v in ("approved", "rejected")
        )
        return {
            "status": "ok",
            "action": action,
            "mode": "by_number",
            "resolved": resolved,
            "details": results,
            "resolved_preview": previews,
            "board_size": len(board),
            "timestamp": datetime.now(UTC).isoformat(),
        }
