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

        # Sacred-board authorization: the hot `follow_up` board is reserved for
        # sanctioned (≈ foreground) paths. An autonomous/dispatched CC session's
        # LLM-authored follow-up is routed to the COLD `tabled` lane — tracked and
        # surfaceable for review, never auto-dispatched; a human/foreground session
        # promotes it to the board if warranted. Root cause of the 2026-07-03
        # fabricated-follow-up incident: an autonomous session minted a board item.
        # (Templated pipelines that call crud.follow_ups.create() directly bypass
        # this tool and are governed at their own call sites.)
        autonomous_routed = False
        if source == "ego_dispatch" and kind != "tabled":
            kind = "tabled"
            autonomous_routed = True

        # The session declares domain when it knows; otherwise fall back to the
        # internal-only classifier (returns 'internal' on a keyword hit, else
        # None → stored NULL, never a user_world guess).
        if domain is None:
            domain = classify_domain(f"{content} {reason}")

        # Provenance: resolve a truncated session id (the per-turn tag shows 8
        # chars) to the full one, the same way session_charter_tools does. A
        # prefix that does not resolve uniquely is stored as NULL, never as the
        # truncated string — resolve_session_id's own contract: "WRITE callers
        # must refuse to create rows for unresolved short ids". The create must
        # not fail over provenance, so the drop is reported, not fatal.
        source_session_note = None
        if source_session:
            from genesis.db.crud import session_charters as _charters

            resolved = await _charters.resolve_session_id(db, source_session)
            if len(resolved) >= 32:
                source_session = resolved
            else:
                source_session_note = (
                    f"source_session '{source_session}' did not resolve to a "
                    "unique full session id — stored NULL rather than a "
                    "truncated id"
                )
                source_session = None

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
        if autonomous_routed:
            lane_msg = (
                "Routed to the tabled (cold) lane: this is an autonomous/dispatched "
                "session, and the hot follow-up board is reserved for sanctioned "
                "(foreground) paths. Tracked and surfaceable for review; a foreground "
                "session can promote it to the board."
            )
        elif kind == "tabled":
            lane_msg = (
                "Tabled (cold list — consciously not pursuing near-term; tracked, not "
                "surfaced as actionable work)."
            )
        else:
            lane_msg = f"Follow-up created (hot list). Strategy: {strategy}."
        return {
            "id": fid,
            "status": "pending",
            "kind": kind,
            "work_state": work_state,
            "revisit_condition": cond,
            "strategy": strategy,
            "domain": domain,
            "pinned": pinned,
            "source_session": source_session,
            "message": (f"NOTE: {source_session_note} " if source_session_note else "")
            + lane_msg
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
            # Count each non-follow_up lane DIRECTLY (not by subtraction, which
            # would lump 'idea' into 'tabled' now that a third kind exists).
            tabled_counts = await follow_ups.get_summary_counts(db, kind="tabled")
            idea_counts = await follow_ups.get_summary_counts(db, kind="idea")
            result["tabled_count"] = sum(tabled_counts.values())
            result["idea_count"] = sum(idea_counts.values())
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
        return {"error": "Database not available", "error_code": "db_unavailable"}

    valid_statuses = {"pending", "scheduled", "in_progress", "completed", "failed", "blocked"}
    if status and status not in valid_statuses:
        return {
            "error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid_statuses))}",
            "error_code": "invalid_status",
        }

    valid_priorities = {"low", "medium", "high", "critical"}
    if priority and priority not in valid_priorities:
        return {
            "error": f"Invalid priority '{priority}'. Must be one of: {', '.join(sorted(valid_priorities))}",
            "error_code": "invalid_priority",
        }

    from genesis.db.crud import follow_ups

    if work_state is not None and work_state not in follow_ups.VALID_WORK_STATE:
        return {
            "error": (
                f"Invalid work_state '{work_state}'. Must be one of: "
                f"{', '.join(sorted(follow_ups.VALID_WORK_STATE))}. See follow_up_create "
                "for the ready / blocked_on_trigger / deferred_cold meanings."
            ),
            "error_code": "invalid_work_state",
        }

    try:
        # Resolve a full id OR a short hex prefix (the proactive hook / memory_expand
        # hand out ``id:<8-char>`` handles, so callers pass them here too). An
        # ambiguous prefix is NEVER guessed; a bare exact miss is loud.
        from genesis.db.crud import _id_resolve

        matches, outcome = await follow_ups.resolve_id(db, follow_up_id)
        resolved_from: str | None = None
        if outcome == _id_resolve.AMBIGUOUS:
            shown = ", ".join(m[:12] for m in matches[:2])
            more = " (and possibly more)" if len(matches) >= 3 else ""
            return {
                "error": (
                    f"Follow-up id '{follow_up_id}' is AMBIGUOUS — it matches {shown}{more}. "
                    "NOTHING was updated. Re-run with a longer id prefix or the full id."
                ),
                "error_code": "ambiguous_id",
            }
        if outcome == _id_resolve.RESOLVED and matches[0] != follow_up_id:
            resolved_from = follow_up_id
            follow_up_id = matches[0]
        elif outcome == _id_resolve.PASSTHROUGH and matches and matches[0] != follow_up_id:
            # A full-length / tagged / whitespace-padded handle (e.g. the
            # ``id:<32hex>`` the proactive hook emits) is normalized by the resolver
            # but not "resolved from a prefix". Adopt the normalized id for the exact
            # lookup — WITHOUT a resolved_from note (it's transparent normalization,
            # not prefix disambiguation). Skipping this leaves the exact lookup on the
            # raw ``id:``-tagged string, which misses though the row exists.
            follow_up_id = matches[0]

        existing = await follow_ups.get_by_id(db, follow_up_id)
        if not existing:
            return {
                "error": (
                    f"No follow-up matches id '{follow_up_id}'. NOTHING was updated — "
                    "the change you intended did NOT happen. Run follow_up_list (or check "
                    "the id against a recent follow_up_create response) and retry with a "
                    "correct id."
                ),
                "error_code": "not_found",
            }

        # Resolve any lane change UP-FRONT (before writes) so a gate failure never
        # leaves a partial update. Lane moves go through work_state (the item's
        # declared state) — there is no raw-`kind` lane override, so priority can't
        # pick the lane on update any more than on create. blocked_on_trigger
        # requires a revisit_condition (newly supplied, or already on the row).
        resolved_kind: str | None = None
        autonomous_promotion_blocked = False
        if work_state is not None:
            resolved_kind = follow_ups.WORK_STATE_TO_KIND[work_state]
            # Sacred-board authorization (mirror of _impl_follow_up_create): an
            # autonomous/dispatched session may NOT PROMOTE a follow-up onto the hot
            # board — only sanctioned (foreground) sessions may. Without this gate an
            # autonomous session could follow_up_create (→ forced tabled) and then
            # follow_up_update(work_state='ready') to flip it straight back onto the
            # board, defeating Part B. Gate ONLY a genuine promotion (off-board → board):
            # a re-affirm of an already-on-board item (existing kind == 'follow_up') is a
            # no-op that must NOT demote it, and moving TO tabled stays allowed.
            if resolved_kind == "follow_up" and existing.get("kind") != "follow_up":
                import os

                if os.environ.get("GENESIS_CC_SESSION") == "1":
                    resolved_kind = "tabled"
                    autonomous_promotion_blocked = True
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
                    ),
                    "error_code": "blocked_on_trigger_needs_revisit",
                }
            if work_state == "blocked_on_trigger" and existing.get("strategy") == "surplus_task":
                return {
                    "error": (
                        "work_state='blocked_on_trigger' can't apply to a surplus_task "
                        "follow-up — surplus tasks dispatch immediately on idle compute, "
                        "ignoring the trigger. Recreate it as scheduled_task (time trigger) "
                        "or user_input_needed / ego_judgment (event trigger)."
                    ),
                    "error_code": "blocked_on_trigger_surplus_conflict",
                }

        # H2 — reject the orphan-making transition INTO status='scheduled'.
        # status='scheduled' is set legitimately only by link_task(), atomically
        # with a linked_task_id. This tool can set the status but not the link, so
        # a manual follow_up_update(status='scheduled') on an UNLINKED row produces
        # a row invisible to every surface (not in get_actionable — which excludes
        # 'scheduled'; not in get_scheduled_due — needs status='pending'+scheduled_at;
        # not in get_linked_active — needs linked_task_id). That is exactly the
        # black hole the July-2026 bake-off row fell into. A row that already
        # carries a linked_task_id stays visible via get_linked_active, so only the
        # unlinked case is blocked. 'blocked' is intentionally NOT gated (it is
        # visible in get_actionable).
        if (
            status == "scheduled"
            and existing.get("status") != "scheduled"
            and not existing.get("linked_task_id")
        ):
            return {
                "error": (
                    "Refusing status='scheduled' on a follow-up with no linked task — "
                    "it would be INVISIBLE to every surface (not actionable, not "
                    "dispatched, not linked-active). NOTHING was updated. To park this "
                    "for later, use work_state='blocked_on_trigger' with a "
                    "revisit_condition (event trigger) or recreate it with "
                    "strategy='scheduled_task' + a scheduled_at (time trigger). "
                    "'scheduled' status is set by the dispatcher, not by hand."
                ),
                "error_code": "scheduled_needs_link",
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
                return {"error": "Update failed — row not modified", "error_code": "not_modified"}
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
        result = {
            "id": follow_up_id,
            "status": refreshed["status"],
            "kind": refreshed.get("kind"),
            "revisit_condition": refreshed.get("revisit_condition"),
            "priority": refreshed["priority"],
            "pinned": bool(refreshed.get("pinned", 0)),
            "message": (
                "Kept on the tabled (cold) lane: an autonomous/dispatched session "
                "cannot promote a follow-up onto the hot board — a foreground session "
                "must do that."
                if autonomous_promotion_blocked
                else "Follow-up updated."
            ),
        }
        if resolved_from is not None:
            # Echo the full id so the caller learns it for subsequent calls.
            result["resolved_from"] = resolved_from
        return result
    except Exception as exc:
        logger.error("follow_up_update failed", exc_info=True)
        return {"error": f"Failed to update follow-up: {exc}", "error_code": "internal_error"}


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
    source_session: str = "",
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
        source_session: which session this work originated from — pass your own
            session id from the per-turn ``[Clock: … | Session: xxxxxxxx]`` tag
            (the 8-char prefix resolves to the full id). Recorded as provenance;
            repo-pulse uses it to attribute completions. A prefix that does not
            resolve uniquely is stored as NULL, never truncated. Leave empty
            only when the origin genuinely is not a CC session.
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
        source_session=source_session or None,
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
        follow_up_id: The follow-up id — a full id OR a short hex prefix (>=4 chars,
            e.g. an 8-char ``id:`` handle from the proactive hook). A unique prefix
            resolves; an ambiguous one is rejected (never guessed); an unknown id
            fails LOUD with error_code='not_found' (the update did NOT happen).
        status: New status (pending, in_progress, completed, failed, blocked). Empty
            to keep current. NOTE: 'scheduled' is set by the dispatcher (link_task),
            not by hand — passing status='scheduled' on an unlinked row is REJECTED
            (error_code='scheduled_needs_link') because it would be invisible to
            every surface. To park for later use work_state='blocked_on_trigger'
            (event) or recreate with strategy='scheduled_task' (time).
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
