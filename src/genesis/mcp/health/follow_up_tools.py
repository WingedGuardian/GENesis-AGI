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
    work_state: str,
    scheduled_at: str | None = None,
    priority: str = "medium",
    pinned: bool = False,
    domain: str | None = None,
    revisit_condition: str = "",
    source_session: str | None = None,
) -> dict:
    """Create a follow-up item in the accountability ledger."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}

    valid_strategies = {"scheduled_task", "surplus_task", "ego_judgment", "user_input_needed"}
    if strategy not in valid_strategies:
        return {
            "error": f"Invalid strategy '{strategy}'. Must be one of: {', '.join(sorted(valid_strategies))}"
        }

    valid_priorities = {"low", "medium", "high", "critical"}
    if priority not in valid_priorities:
        return {
            "error": f"Invalid priority '{priority}'. Must be one of: {', '.join(sorted(valid_priorities))}"
        }

    valid_domains = {"internal", "user_world"}
    if domain is not None and domain not in valid_domains:
        return {
            "error": f"Invalid domain '{domain}'. Must be one of: {', '.join(sorted(valid_domains))}"
        }

    from genesis.db.crud import follow_ups

    if work_state not in follow_ups.VALID_WORK_STATE:
        return {
            "error": (
                f"Invalid work_state '{work_state}'. Must be one of: "
                f"{', '.join(sorted(follow_ups.VALID_WORK_STATE))}. "
                "ready = actionable now, just needs doing (manpower) → follow_up (hot list). "
                "blocked_on_trigger = intended but waiting on a specific time/event/"
                "precondition → follow_up (hot list); requires revisit_condition. "
                "deferred_cold = consciously NOT pursuing near-term (vague/hard/someday) "
                "→ tabled (cold list). This is an intent axis, NOT priority — a "
                "low-priority item you still intend to do is 'ready', not 'deferred_cold'."
            )
        }
    kind = follow_ups.WORK_STATE_TO_KIND[work_state]
    if work_state == "blocked_on_trigger" and not revisit_condition.strip():
        return {
            "error": (
                "work_state='blocked_on_trigger' requires revisit_condition — name the "
                "specific time/event/precondition you're waiting on. If there is no "
                "concrete trigger and you're just not pursuing this near-term, use "
                "work_state='deferred_cold' (tabled). If it needs doing now, use "
                "work_state='ready'."
            )
        }
    if work_state == "blocked_on_trigger" and strategy == "surplus_task":
        # surplus_task dispatches immediately on idle compute (dispatcher.py:63-70),
        # which would run the work BEFORE the trigger fires. A prose trigger can't
        # gate an immediate dispatch — steer to a strategy that honors waiting.
        return {
            "error": (
                "work_state='blocked_on_trigger' cannot use strategy='surplus_task' — "
                "surplus tasks dispatch immediately on idle compute and would run before "
                "the trigger fires. Use strategy='scheduled_task' (with scheduled_at) for "
                "a time trigger, or 'user_input_needed' / 'ego_judgment' for an event "
                "trigger so it isn't auto-dispatched before the event."
            )
        }
    if work_state == "ready":
        revisit_condition = ""  # a 'ready' item has no trigger — never store one

    if strategy == "scheduled_task" and not scheduled_at:
        return {"error": "scheduled_at is required when strategy is 'scheduled_task'"}

    try:
        import os

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
            revisit_condition=revisit_condition or None,
        )
        cond = revisit_condition.strip() or None
        lane_msg = (
            "Tabled (cold list — consciously not pursuing near-term; tracked, not "
            "surfaced as actionable work)."
            if kind == "tabled"
            else f"Follow-up created (hot list). Strategy: {strategy}."
        )
        return {
            "id": fid,
            "status": "pending",
            "kind": kind,
            "work_state": work_state,
            "revisit_condition": cond,
            "strategy": strategy,
            "domain": domain,
            "pinned": pinned,
            "message": lane_msg
            + (f" Revisit when: {cond}." if cond else "")
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
                db,
                status_filter,
                include_tabled=include_tabled,
            )
        else:
            items = await follow_ups.get_recent(
                db,
                limit=limit,
                include_tabled=include_tabled,
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
    work_state: str | None = None,
    revisit_condition: str | None = None,
) -> dict:
    """Update an existing follow-up item."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}

    valid_statuses = {"pending", "scheduled", "in_progress", "completed", "failed", "blocked"}
    if status and status not in valid_statuses:
        return {
            "error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid_statuses))}"
        }

    valid_priorities = {"low", "medium", "high", "critical"}
    if priority and priority not in valid_priorities:
        return {
            "error": f"Invalid priority '{priority}'. Must be one of: {', '.join(sorted(valid_priorities))}"
        }

    from genesis.db.crud import follow_ups

    if work_state is not None and work_state not in follow_ups.VALID_WORK_STATE:
        return {
            "error": (
                f"Invalid work_state '{work_state}'. Must be one of: "
                f"{', '.join(sorted(follow_ups.VALID_WORK_STATE))}. See follow_up_create "
                "for the ready / blocked_on_trigger / deferred_cold meanings."
            )
        }

    try:
        existing = await follow_ups.get_by_id(db, follow_up_id)
        if not existing:
            return {"error": f"Follow-up '{follow_up_id}' not found"}

        # Resolve any lane change UP-FRONT (before writes) so a gate failure never
        # leaves a partial update. Lane moves go through work_state (the item's
        # declared state) — there is no raw-`kind` lane override, so priority can't
        # pick the lane on update any more than on create. blocked_on_trigger
        # requires a revisit_condition (newly supplied, or already on the row).
        resolved_kind: str | None = None
        if work_state is not None:
            resolved_kind = follow_ups.WORK_STATE_TO_KIND[work_state]
            effective_cond = (
                revisit_condition
                if revisit_condition is not None
                else existing.get("revisit_condition")
            ) or ""
            if work_state == "blocked_on_trigger" and not effective_cond.strip():
                return {
                    "error": (
                        "work_state='blocked_on_trigger' requires revisit_condition — "
                        "name the time/event/precondition you're waiting on, or use "
                        "'deferred_cold' (tabled) / 'ready' instead."
                    )
                }
            if work_state == "blocked_on_trigger" and existing.get("strategy") == "surplus_task":
                return {
                    "error": (
                        "work_state='blocked_on_trigger' can't apply to a surplus_task "
                        "follow-up — surplus tasks dispatch immediately on idle compute, "
                        "ignoring the trigger. Recreate it as scheduled_task (time trigger) "
                        "or user_input_needed / ego_judgment (event trigger)."
                    )
                }

        if priority and priority != existing.get("priority"):
            await db.execute(
                "UPDATE follow_ups SET priority = ? WHERE id = ?",
                (priority, follow_up_id),
            )
            await db.commit()

        if pinned is not None:
            await follow_ups.set_pinned(db, follow_up_id, pinned)

        if resolved_kind:
            await follow_ups.set_kind(db, follow_up_id, resolved_kind)
        if work_state == "ready":
            # a 'ready' item has no trigger — clear any stale revisit_condition
            await follow_ups.set_revisit_condition(db, follow_up_id, None)
        elif revisit_condition is not None:
            await follow_ups.set_revisit_condition(db, follow_up_id, revisit_condition)

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
        elif blocked_reason is not None:
            # blocked_reason without an explicit status means "block this" — honor
            # the documented contract (it previously silently kept the existing
            # status). Any notes ride along on the same targeted status write.
            await follow_ups.update_status(
                db,
                follow_up_id,
                "blocked",
                resolution_notes=resolution_notes,
                blocked_reason=blocked_reason,
            )
        elif resolution_notes is not None:
            # Notes-only update: write ONLY resolution_notes, never re-touch
            # status. Re-applying the status read at the top of this call races
            # Genesis's own live writers and silently reverts their change
            # (lost update, follow-up d67c83c7).
            await follow_ups.update_notes(
                db,
                follow_up_id,
                resolution_notes=resolution_notes,
            )

        refreshed = await follow_ups.get_by_id(db, follow_up_id)
        return {
            "id": follow_up_id,
            "status": refreshed["status"],
            "kind": refreshed.get("kind"),
            "revisit_condition": refreshed.get("revisit_condition"),
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
    work_state: str,
    scheduled_at: str = "",
    priority: str = "medium",
    pinned: bool = False,
    domain: str = "",
    revisit_condition: str = "",
) -> dict:
    """Create a follow-up in the accountability ledger.

    Declare the item's WORK_STATE — the tool DERIVES the lane from it, so priority
    never decides the lane. Two lists:
    - HOT (follow_up): work you INTEND to do near-term. May be blocked on time, an
      event, or just manpower — but NEVER hard-blocked / not-an-easy-fix / vague.
    - COLD (tabled): things you are CONSCIOUSLY NOT doing near-term (further off,
      harder, vaguer — maybe someday). Kept off the actionable queue.

    Args:
        content: What needs to happen (actionable description).
        reason: Why this follow-up exists (context for future sessions/ego).
        work_state: The item's actual state — this DERIVES the lane. Pick honestly;
            it is an intent/tractability axis, NOT priority (a low-priority item you
            still intend to do is 'ready', not 'deferred_cold'):
            - "ready": actionable now, just needs doing → HOT (follow_up).
            - "blocked_on_trigger": intended, waiting on a specific time/event/
              precondition → HOT (follow_up). REQUIRES revisit_condition. Not valid
              with strategy="surplus_task" (that dispatches immediately, ignoring the
              trigger) — use scheduled_task (time) or user_input_needed/ego_judgment (event).
            - "deferred_cold": consciously not pursuing near-term (vague/hard/
              someday) → COLD (tabled).
        strategy: How to EXECUTE it if/when acted on (orthogonal to work_state):
            - user_input_needed: park for a future interactive CC session (coding,
              plan execution, Genesis dev, file edits). Surfaces in morning report.
            - surplus_task: enqueue to the free-model surplus system — pure analysis/
              summarization only, never code/file edits or interactive work.
            - scheduled_task: like surplus_task but time-triggered (the time-based
              form of blocked_on_trigger); requires scheduled_at. Free model only.
            - ego_judgment: hand to ego to evaluate next cycle (a good default for
              deferred_cold items, which have no near-term execution route).
        revisit_condition: The trigger to revisit — REQUIRED when
            work_state="blocked_on_trigger" (name the time/event/precondition).
            Optional but encouraged for deferred_cold (what would revive it).
        scheduled_at: ISO datetime (required when strategy is scheduled_task).
        priority: low | medium | high | critical. Does NOT affect the lane.
        pinned: If true, ego can see but cannot auto-resolve; only the user closes it.
        domain: "internal" (Genesis's own system work) or "user_world" (the user's
            life/career/content). Leave empty to let Genesis classify (internal-only).
    """
    return await _impl_follow_up_create(
        content,
        reason,
        strategy,
        work_state=work_state,
        scheduled_at=scheduled_at or None,
        priority=priority,
        pinned=pinned,
        domain=domain or None,
        revisit_condition=revisit_condition,
    )


@mcp.tool()
async def follow_up_update(
    follow_up_id: str,
    status: str = "",
    resolution_notes: str = "",
    blocked_reason: str = "",
    priority: str = "",
    pinned: str = "",
    work_state: str = "",
    revisit_condition: str = "",
) -> dict:
    """Update an existing follow-up item.

    Change status, add resolution notes, mark blocked, adjust priority, pin/unpin,
    or move it between the hot (follow_up) and cold (tabled) lanes.

    Args:
        follow_up_id: The ID of the follow-up to update.
        status: New status (pending, scheduled, in_progress, completed, failed, blocked). Empty to keep current.
        resolution_notes: Notes on resolution or progress. Appended context for future sessions.
        blocked_reason: Why this follow-up is blocked (sets status to blocked if status not provided).
        priority: New priority (low, medium, high, critical). Empty to keep current.
        pinned: "true" to pin (ego cannot auto-resolve) or "false" to unpin. Empty to keep current.
        work_state: Re-declare the item's state to move lanes:
            "ready"/"blocked_on_trigger" → hot (follow_up); "deferred_cold" → cold
            (tabled). blocked_on_trigger requires revisit_condition (new or already
            set). Empty to keep the current lane.
        revisit_condition: Set/replace the revisit trigger (the event that would
            resurface the item). Empty to leave unchanged.
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
        work_state=work_state or None,
        revisit_condition=revisit_condition or None,
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
