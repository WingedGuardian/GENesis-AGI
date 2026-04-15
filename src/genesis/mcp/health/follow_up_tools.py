"""MCP tools for the follow-up accountability ledger.

Provides follow_up_create and follow_up_list for foreground sessions
to create and inspect follow-up items.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _get_db():
    """Late-import DB from the health MCP module state."""
    import genesis.mcp.health_mcp as health_mcp_mod

    svc = health_mcp_mod._service
    if svc is None:
        return None
    return getattr(svc, "_db", None)


# ---------------------------------------------------------------------------
# Implementation functions (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_follow_up_create(
    content: str,
    reason: str,
    strategy: str,
    *,
    scheduled_at: str | None = None,
    priority: str = "medium",
    source_session: str | None = None,
) -> dict:
    """Create a follow-up item in the accountability ledger."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}

    valid_strategies = {"scheduled_task", "surplus_task", "ego_judgment", "user_input_needed"}
    if strategy not in valid_strategies:
        return {"error": f"Invalid strategy '{strategy}'. Must be one of: {', '.join(sorted(valid_strategies))}"}

    valid_priorities = {"low", "medium", "high", "critical"}
    if priority not in valid_priorities:
        return {"error": f"Invalid priority '{priority}'. Must be one of: {', '.join(sorted(valid_priorities))}"}

    if strategy == "scheduled_task" and not scheduled_at:
        return {"error": "scheduled_at is required when strategy is 'scheduled_task'"}

    try:
        from genesis.db.crud import follow_ups

        fid = await follow_ups.create(
            db,
            content=content,
            source="foreground_session",
            source_session=source_session,
            reason=reason,
            strategy=strategy,
            scheduled_at=scheduled_at,
            priority=priority,
        )
        return {
            "id": fid,
            "status": "pending",
            "strategy": strategy,
            "message": f"Follow-up created. Strategy: {strategy}.",
        }
    except Exception as exc:
        logger.error("follow_up_create failed", exc_info=True)
        return {"error": f"Failed to create follow-up: {exc}"}


async def _impl_follow_up_list(
    status_filter: str | None = None,
    limit: int = 20,
) -> dict:
    """List follow-up items, optionally filtered by status."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}

    try:
        from genesis.db.crud import follow_ups

        if status_filter:
            items = await follow_ups.get_by_status(db, status_filter)
        else:
            items = await follow_ups.get_recent(db, limit=limit)

        counts = await follow_ups.get_summary_counts(db)

        return {
            "follow_ups": items[:limit],
            "counts": counts,
            "total": sum(counts.values()),
        }
    except Exception as exc:
        logger.error("follow_up_list failed", exc_info=True)
        return {"error": f"Failed to list follow-ups: {exc}"}


# ---------------------------------------------------------------------------
# MCP tool decorators
# ---------------------------------------------------------------------------


@mcp.tool()
async def follow_up_create(
    content: str,
    reason: str,
    strategy: str,
    scheduled_at: str = "",
    priority: str = "medium",
) -> dict:
    """Create a follow-up item for Genesis to track and execute.

    Use this when a session identifies deferred work that Genesis should own.

    Args:
        content: What needs to happen (actionable description)
        reason: Why this follow-up exists (context for future sessions/ego)
        strategy: How to handle it:
            - scheduled_task: Run at scheduled_at time via surplus
            - surplus_task: Run ASAP via surplus scheduler
            - ego_judgment: Needs ego evaluation in next cycle
            - user_input_needed: Requires user decision, surfaced in morning report
        scheduled_at: ISO datetime for scheduled_task strategy (required if strategy is scheduled_task)
        priority: low | medium | high | critical
    """
    return await _impl_follow_up_create(
        content,
        reason,
        strategy,
        scheduled_at=scheduled_at or None,
        priority=priority,
    )


@mcp.tool()
async def follow_up_list(
    status_filter: str = "",
    limit: int = 20,
) -> dict:
    """List follow-up items with status counts.

    Args:
        status_filter: Filter by status (pending, scheduled, in_progress, completed, failed, blocked). Empty for all.
        limit: Max items to return (default 20)
    """
    return await _impl_follow_up_list(
        status_filter=status_filter or None,
        limit=limit,
    )
