"""MCP tools for ego management — focus reset, directives, goal CRUD.

Allows foreground CC sessions to interact with the ego system:
- Reset focus (break holdback loops)
- Create directives (flag things as important for the ego)
- Manage goals (create, list, update, achieve, abandon)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


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
    import aiosqlite

    from genesis.db.crud import ego as ego_crud
    from genesis.ego.session import _BEHAVIORAL_FOCUS_RE

    default_focus = "general system awareness"
    focus_to_set = new_focus.strip() if new_focus else default_focus

    if _BEHAVIORAL_FOCUS_RE.search(focus_to_set):
        return {
            "status": "rejected",
            "reason": (
                "The provided focus describes a behavioral state, not a topic. "
                "Focus must describe what to think ABOUT, not how to behave."
            ),
        }

    results = {}
    db_path = _get_db_path()

    async with aiosqlite.connect(str(db_path)) as db:
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
    import aiosqlite

    from genesis.db.crud import ego as ego_crud

    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
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


@mcp.tool()
async def ego_goal_create(
    title: str,
    category: str = "project",
    priority: str = "medium",
    description: str = "",
    timeline: str = "",
) -> dict:
    """Create a new user goal for the ego system.

    Goals are visible to the ego in its thinking cycles. The ego considers
    goals when deciding what to propose.

    Args:
        title: Goal title (concise, actionable)
        category: career, project, learning, relationship, financial, other
        priority: low, medium, high, or critical
        description: Detailed description of the goal
        timeline: Expected timeline (e.g., "Q2 2026")
    """
    import aiosqlite

    from genesis.db.crud import user_goals

    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        goal_id = await user_goals.create(
            db,
            title=title,
            category=category,
            priority=priority,
            description=description,
            timeline=timeline or None,
            confidence=0.9,
        )

    return {
        "status": "created",
        "goal_id": goal_id,
        "title": title,
        "category": category,
        "priority": priority,
    }


@mcp.tool()
async def ego_goal_list() -> dict:
    """List all active user goals in the ego system."""
    import aiosqlite

    from genesis.db.crud import user_goals

    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
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
) -> dict:
    """Update an existing user goal, or mark it as achieved/abandoned.

    Pass only the fields you want to change. Empty strings are ignored.

    Args:
        goal_id: The goal ID to update
        title: New title (optional)
        category: New category (optional)
        priority: New priority (optional)
        description: New description (optional)
        timeline: New timeline (optional)
        status: Set to 'achieved' or 'abandoned' to close the goal (optional)
    """
    import aiosqlite

    from genesis.db.crud import user_goals

    db_path = _get_db_path()
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row

        if status == "achieved":
            await user_goals.mark_achieved(db, goal_id)
            return {"status": "achieved", "goal_id": goal_id}
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
        if not fields:
            return {"status": "error", "reason": "no fields to update"}

        await user_goals.update(db, goal_id, **fields)
        return {"status": "updated", "goal_id": goal_id, "fields": list(fields.keys())}
