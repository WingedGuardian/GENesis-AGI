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
    domain: str | None = None,
    kind: str = "follow_up",
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

    valid_domains = {"internal", "user_world"}
    if domain is not None and domain not in valid_domains:
        return {"error": f"Invalid domain '{domain}'. Must be one of: {', '.join(sorted(valid_domains))}"}

    valid_kinds = {"follow_up", "tabled"}
    if kind not in valid_kinds:
        return {"error": f"Invalid kind '{kind}'. Must be one of: {', '.join(sorted(valid_kinds))}"}

    if strategy == "scheduled_task" and not scheduled_at:
        return {"error": "scheduled_at is required when strategy is 'scheduled_task'"}

    try:
        import os

        from genesis.db.crud import follow_ups
        from genesis.ego.domain_classifier import classify_domain

        # Detect dispatched session context for proper source attribution
        if os.environ.get("GENESIS_CC_SESSION") == "1":
            source = "ego_dispatch"
        else:
            source = "foreground_session"

        # The session declares domain when it knows; otherwise fall back to the
        # internal-only classifier (returns 'internal' on a keyword hit, else
        # None → stored NULL, never a user_world guess).
        if domain is None:
            domain = classify_domain(f"{content} {reason}")

        fid = await follow_ups.create(
            db,
            content=content,
            source=source,
            source_session=source_session,
            reason=reason,
            strategy=strategy,
            scheduled_at=scheduled_at,
            priority=priority,
            pinned=pinned,
            domain=domain,
            kind=kind,
        )
        lane_msg = (
            "Tabled (someday/maybe — tracked, not surfaced as actionable work)."
            if kind == "tabled"
            else f"Follow-up created. Strategy: {strategy}."
        )
        return {
            "id": fid,
            "status": "pending",
            "kind": kind,
            "strategy": strategy,
            "domain": domain,
            "pinned": pinned,
            "message": lane_msg
            + (f" Domain: {domain}." if domain else "")
            + (" (pinned — ego cannot auto-resolve)" if pinned else ""),
        }
    except Exception as exc:
        logger.error("follow_up_create failed", exc_info=True)
        return {"error": f"Failed to create follow-up: {exc}"}


async def _impl_follow_up_list(
    status_filter: str | None = None,
    limit: int = 20,
    include_tabled: bool = False,
) -> dict:
    """List follow-up items, optionally filtered by status.

    By default the tabled (someday/maybe) lane is excluded so the list and
    counts reflect actionable work; ``tabled_count`` reports how many are
    shelved. Pass include_tabled=True to include tabled items in the list.
    """
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}

    try:
        from genesis.db.crud import follow_ups

        if status_filter:
            items = await follow_ups.get_by_status(
                db, status_filter, include_tabled=include_tabled,
            )
        else:
            items = await follow_ups.get_recent(
                db, limit=limit, include_tabled=include_tabled,
            )

        counts = await follow_ups.get_summary_counts(db, include_tabled=include_tabled)

        result = {
            "follow_ups": items[:limit],
            "counts": counts,
            "total": sum(counts.values()),
        }
        if not include_tabled:
            all_counts = await follow_ups.get_summary_counts(db, include_tabled=True)
            result["tabled_count"] = sum(all_counts.values()) - sum(counts.values())
        return result
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
    kind: str | None = None,
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

    valid_kinds = {"follow_up", "tabled"}
    if kind and kind not in valid_kinds:
        return {"error": f"Invalid kind '{kind}'. Must be one of: {', '.join(sorted(valid_kinds))}"}

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

        if kind:
            await follow_ups.set_kind(db, follow_up_id, kind)

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
            # Notes-only update: write ONLY the note columns. Do NOT re-assert
            # existing["status"] (read at entry) -- a concurrent status
            # transition landing between that read and this write would be
            # reverted (lost update). See crud.follow_ups.update_notes.
            await follow_ups.update_notes(
                db,
                follow_up_id,
                resolution_notes=resolution_notes,
                blocked_reason=blocked_reason,
            )

        refreshed = await follow_ups.get_by_id(db, follow_up_id)
        return {
            "id": follow_up_id,
            "status": refreshed["status"],
            "kind": refreshed.get("kind"),
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
    domain: str = "",
    kind: str = "follow_up",
) -> dict:
    """Create a follow-up (or a tabled someday/maybe) in the accountability ledger.

    Two lanes, chosen by `kind` — pick deliberately:
    - kind="follow_up" (default): ACTIONABLE deferred work Genesis should own and
      eventually DO. Enters the ledger, surfaces in the morning report, and the
      ego/sessions act on it. Use ONLY when there is a real, intended next step.
    - kind="tabled": a SOMEDAY/MAYBE — worth remembering but NOT committing to.
      Tracked, but never surfaced as work or auto-actioned. Use for ideas,
      interests, or possibilities to revisit later so they don't clog the
      actionable queue. When torn between a low-priority follow_up and a maybe
      with no concrete next step, prefer tabled.

    Args:
        content: What needs to happen (actionable description)
        reason: Why this follow-up exists (context for future sessions/ego)
        kind: "follow_up" (actionable, default) or "tabled" (someday/maybe, never
            auto-actioned). See the two-lane note above — don't file a real
            commitment as tabled, and don't clog the actionable queue with maybes.
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
        domain: Whose world this belongs to — "internal" (Genesis's own system
            work: runtime, routing, memory, health, dev) or "user_world" (the
            user's life, career, content, interests). Leave empty to let Genesis
            classify (it only auto-detects internal; otherwise leaves it unset).
    """
    return await _impl_follow_up_create(
        content,
        reason,
        strategy,
        scheduled_at=scheduled_at or None,
        priority=priority,
        pinned=pinned,
        domain=domain or None,
        kind=kind,
    )


@mcp.tool()
async def follow_up_update(
    follow_up_id: str,
    status: str = "",
    resolution_notes: str = "",
    blocked_reason: str = "",
    priority: str = "",
    pinned: str = "",
    kind: str = "",
) -> dict:
    """Update an existing follow-up item.

    Use this to change status, add resolution notes, mark as blocked,
    adjust priority, pin/unpin, or move it between the follow_up/tabled lanes.

    Args:
        follow_up_id: The ID of the follow-up to update
        status: New status (pending, scheduled, in_progress, completed, failed, blocked). Empty to keep current.
        resolution_notes: Notes on resolution or progress. Appended context for future sessions.
        blocked_reason: Why this follow-up is blocked (sets status to blocked if status not provided).
        priority: New priority (low, medium, high, critical). Empty to keep current.
        pinned: Set to "true" to pin (ego cannot auto-resolve) or "false" to unpin. Empty to keep current.
        kind: Move between lanes — "follow_up" (actionable) or "tabled" (someday/maybe).
            Empty to keep current. Use to demote a follow-up you're no longer
            committing to into tabled, or promote a tabled idea back to actionable work.
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
        kind=kind or None,
    )


@mcp.tool()
async def follow_up_list(
    status_filter: str = "",
    limit: int = 20,
    include_tabled: bool = False,
) -> dict:
    """List follow-up items with status counts.

    By default only the actionable follow_up lane is listed; the response's
    ``tabled_count`` says how many someday/maybe items are shelved. Set
    include_tabled=True to include tabled items in the list itself.

    Args:
        status_filter: Filter by status (pending, scheduled, in_progress, completed, failed, blocked). Empty for all.
        limit: Max items to return (default 20)
        include_tabled: Include tabled (someday/maybe) items in the list. Default False.
    """
    return await _impl_follow_up_list(
        status_filter=status_filter or None,
        limit=limit,
        include_tabled=include_tabled,
    )
