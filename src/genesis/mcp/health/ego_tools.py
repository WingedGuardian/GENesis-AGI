"""MCP tool for resetting ego focus state.

Allows a foreground CC session to clear behavioral self-assignments
from the ego's focus_summary, breaking self-reinforcing holdback loops.
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
