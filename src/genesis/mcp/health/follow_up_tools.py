"""MCP tools for the follow-up accountability ledger.

Provides follow_up_create, follow_up_list, and follow_up_update for
foreground sessions to create, inspect, and update follow-up items.
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
    pinned: bool = False,
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
            pinned=pinned,
        )
        return {
            "id": fid,
            "status": "pending",
            "strategy": strategy,
            "pinned": pinned,
            "message": f"Follow-up created. Strategy: {strategy}."
            + (" (pinned — ego cannot auto-resolve)" if pinned else ""),
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


async def _impl_follow_up_update(
    follow_up_id: str,
    *,
    status: str | None = None,
    resolution_notes: str | None = None,
    blocked_reason: str | None = None,
    priority: str | None = None,
    pinned: bool | None = None,
) -> dict:
    """Update an existing follow-up item."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}

    valid_statuses = {"pending", "scheduled", "in_progress", "completed", "failed", "blocked"}
    if status and status not in valid_statuses:
        return {"error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid_statuses))}"}

    valid_priorities = {"low", "medium", "high", "critical"}
    if priority and priority not in valid_priorities:
        return {"error": f"Invalid priority '{priority}'. Must be one of: {', '.join(sorted(valid_priorities))}"}

    try:
        from genesis.db.crud import follow_ups

        existing = await follow_ups.get_by_id(db, follow_up_id)
        if not existing:
            return {"error": f"Follow-up '{follow_up_id}' not found"}

        if priority and priority != existing.get("priority"):
            await db.execute(
                "UPDATE follow_ups SET priority = ? WHERE id = ?",
                (priority, follow_up_id),
            )
            await db.commit()

        if pinned is not None:
            await follow_ups.set_pinned(db, follow_up_id, pinned)

        if status:
            updated = await follow_ups.update_status(
                db,
                follow_up_id,
                status,
                resolution_notes=resolution_notes,
                blocked_reason=blocked_reason,
            )
            if not updated:
                return {"error": "Update failed — row not modified"}
        elif resolution_notes or blocked_reason:
            await follow_ups.update_status(
                db,
                follow_up_id,
                existing["status"],
                resolution_notes=resolution_notes,
                blocked_reason=blocked_reason,
            )

        refreshed = await follow_ups.get_by_id(db, follow_up_id)
        return {
            "id": follow_up_id,
            "status": refreshed["status"],
            "priority": refreshed["priority"],
            "pinned": bool(refreshed.get("pinned", 0)),
            "message": "Follow-up updated.",
        }
    except Exception as exc:
        logger.error("follow_up_update failed", exc_info=True)
        return {"error": f"Failed to update follow-up: {exc}"}


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
    pinned: bool = False,
) -> dict:
    """Create a follow-up item for Genesis to track and execute.

    Use this when a session identifies deferred work that Genesis should own.

    Args:
        content: What needs to happen (actionable description)
        reason: Why this follow-up exists (context for future sessions/ego)
        strategy: How to handle it — choose based on what kind of work this requires:
            - user_input_needed: Park this for a future interactive session. No
              automation touches it. Use for anything requiring real CC sessions:
              coding, plan execution, Genesis development, file edits. Surfaces in
              morning report so the user can trigger a session when ready.
            - surplus_task: Enqueue to the free-model surplus system. Runs on idle
              compute using lightweight free-tier models. ONLY for pure analysis,
              summarization, or data processing. NOT for code changes, file edits,
              or anything requiring an interactive CC session.
            - scheduled_task: Same as surplus_task but triggered at a specific time.
              Same constraints — free model only, no interactive work.
            - ego_judgment: Hand to ego for evaluation in its next cycle. Ego decides
              whether to act, defer, or escalate. Not auto-executed.
        scheduled_at: ISO datetime for scheduled_task strategy (required if strategy is scheduled_task)
        priority: low | medium | high | critical
        pinned: If true, ego can see this follow-up but cannot auto-resolve it.
            Only the user can close a pinned follow-up. Use for items you want
            to track personally.
    """
    return await _impl_follow_up_create(
        content,
        reason,
        strategy,
        scheduled_at=scheduled_at or None,
        priority=priority,
        pinned=pinned,
    )


@mcp.tool()
async def follow_up_update(
    follow_up_id: str,
    status: str = "",
    resolution_notes: str = "",
    blocked_reason: str = "",
    priority: str = "",
    pinned: str = "",
) -> dict:
    """Update an existing follow-up item.

    Use this to change status, add resolution notes, mark as blocked,
    adjust priority, or pin/unpin on an existing follow-up.

    Args:
        follow_up_id: The ID of the follow-up to update
        status: New status (pending, scheduled, in_progress, completed, failed, blocked). Empty to keep current.
        resolution_notes: Notes on resolution or progress. Appended context for future sessions.
        blocked_reason: Why this follow-up is blocked (sets status to blocked if status not provided).
        priority: New priority (low, medium, high, critical). Empty to keep current.
        pinned: Set to "true" to pin (ego cannot auto-resolve) or "false" to unpin. Empty to keep current.
    """
    pinned_bool: bool | None = None
    if pinned.lower() in ("true", "1", "yes"):
        pinned_bool = True
    elif pinned.lower() in ("false", "0", "no"):
        pinned_bool = False

    return await _impl_follow_up_update(
        follow_up_id,
        status=status or None,
        resolution_notes=resolution_notes or None,
        blocked_reason=blocked_reason or None,
        priority=priority or None,
        pinned=pinned_bool,
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
