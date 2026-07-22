"""Unified Communications route — aggregates outreach, ego proposals, and approvals."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


async def _enrich_proposals_with_outcomes(
    db: object, proposals: list[dict],
) -> list[dict]:
    """For executed proposals, join session outcome from cc_sessions."""
    session_ids: list[tuple[int, str]] = []
    for i, p in enumerate(proposals):
        ur = p.get("user_response") or ""
        if p.get("status") == "executed" and ur.startswith("session:"):
            session_ids.append((i, ur[8:]))

    if not session_ids:
        return proposals

    # Batch fetch session outcomes
    placeholders = ",".join("?" for _ in session_ids)
    ids = [sid for _, sid in session_ids]
    try:
        cursor = await db.execute(
            f"SELECT id, status, cost_usd, completed_at, metadata "
            f"FROM cc_sessions WHERE id IN ({placeholders})",
            ids,
        )
        rows = {r[0]: r for r in await cursor.fetchall()}
    except Exception:
        logger.debug("Failed to fetch session outcomes for proposals", exc_info=True)
        return proposals

    for idx, sid in session_ids:
        row = rows.get(sid)
        if not row:
            continue
        meta = {}
        if row[4]:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                meta = json.loads(row[4])
        output_text = meta.get("output_text", "")
        error = meta.get("error", "")
        proposals[idx]["session_outcome"] = {
            "session_id": sid,
            "status": row[1],
            "cost_usd": row[2],
            "completed_at": row[3],
            "output_summary": (output_text[:300] + "...") if len(output_text) > 300 else output_text,
            "error": (error[:200] + "...") if len(error) > 200 else error,
            "profile": meta.get("profile", ""),
        }
    return proposals


@blueprint.route("/api/genesis/comms")
@_async_route
async def unified_comms():
    """Return unified communications data: outreach, proposals, and approvals.

    Query params:
        view  – 'pending' (default) or 'all'
        limit – max items per source (default 30, capped at 100)
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({
            "outreach": [], "proposals": [], "pending_approvals": [],
            "counts": {},
        })

    view = request.args.get("view", "pending")
    limit = max(1, min(request.args.get("limit", 30, type=int), 100))

    outreach: list[dict] = []
    proposals: list[dict] = []
    pending_approvals: list[dict] = []
    counts: dict = {}

    # --- Outreach messages ---
    try:
        cursor = await rt.db.execute(
            """SELECT id, category, signal_type, topic, channel, message_content,
                      delivered_at, engagement_outcome, user_response, created_at
               FROM outreach_history
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        )
        cols = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        outreach = [dict(zip(cols, r, strict=False)) for r in rows]

        count_cursor = await rt.db.execute("SELECT COUNT(*) FROM outreach_history")
        counts["outreach_total"] = (await count_cursor.fetchone())[0]
    except Exception:
        logger.error("Failed to fetch outreach for comms view", exc_info=True)
        counts["outreach_total"] = 0

    # --- Ego proposals ---
    informational: list[dict] = []
    try:
        from genesis.db.crud import ego
        from genesis.ego.types import (
            INFORMATIONAL_ACTION_TYPES,
            partition_informational,
        )

        if view == "pending":
            raw = await ego.list_pending_proposals(rt.db)
            # Acknowledge-only eval rows (j9/gauntlet) are notifications, not
            # approval items — split them into a separate informational lane.
            proposals, informational = partition_informational(raw)
        else:
            proposals = await ego.list_proposals(rt.db, limit=limit)

        # Enrich executed proposals with session outcome data
        proposals = await _enrich_proposals_with_outcomes(rt.db, proposals)

        # Pending APPROVAL count excludes informational rows (they carry no
        # approve/reject decision). Built from the shared constant so it can
        # never drift from the lane split above.
        _info_placeholders = ",".join("?" for _ in INFORMATIONAL_ACTION_TYPES)
        pending_count_cursor = await rt.db.execute(
            "SELECT COUNT(*) FROM ego_proposals WHERE status = 'pending' "
            f"AND action_type NOT IN ({_info_placeholders})",
            tuple(INFORMATIONAL_ACTION_TYPES),
        )
        counts["proposals_pending"] = (await pending_count_cursor.fetchone())[0]
        counts["proposals_informational"] = len(informational)

        total_count_cursor = await rt.db.execute(
            "SELECT COUNT(*) FROM ego_proposals"
        )
        counts["proposals_total"] = (await total_count_cursor.fetchone())[0]
    except Exception:
        # Ego tables may be empty if ego hasn't run — that's fine
        logger.debug("Ego proposals unavailable for comms view", exc_info=True)
        counts["proposals_pending"] = 0
        counts["proposals_total"] = 0

    # --- Pending approvals ---
    try:
        from genesis.db.crud import approval_requests

        raw_pending = await approval_requests.list_pending(rt.db)
        # Normalize rows — list_pending may return Row objects
        for row in raw_pending:
            if hasattr(row, "keys"):
                pending_approvals.append(dict(row))
            elif isinstance(row, dict):
                pending_approvals.append(row)
        counts["pending_approvals"] = len(pending_approvals)
    except Exception:
        logger.error("Failed to fetch approvals for comms view", exc_info=True)
        counts["pending_approvals"] = 0

    return jsonify({
        "outreach": outreach,
        "proposals": proposals,
        "informational": informational,
        "pending_approvals": pending_approvals,
        "counts": counts,
    })


@blueprint.route("/api/genesis/comms/proposals/<proposal_id>/resolve", methods=["POST"])
@_async_route
async def comms_resolve_proposal(proposal_id: str):
    """Approve or reject an ego proposal from the Chat/Comms tab."""
    from genesis.db.crud import ego
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip().lower()
    if status not in ("approved", "rejected"):
        return jsonify({"error": "status must be 'approved' or 'rejected'"}), 400

    user_response = data.get("user_response", "")

    ok = await ego.resolve_proposal(
        rt.db,
        proposal_id,
        status=status,
        user_response=user_response or None,
    )
    if not ok:
        return jsonify({"error": "Proposal not found or already resolved"}), 404

    # Shared post-resolution hook: journal + J-9 + decision capture +
    # correction memory + ALL action hooks (this route previously ran only
    # 4 of the 6 and no J-9/journal parity — the fifth resolution path).
    prop = None
    try:
        prop = await ego.get_proposal(rt.db, proposal_id)
    except Exception:
        logger.warning("could not load proposal %s for resolution hooks", proposal_id)
    if prop:
        try:
            from genesis.ego.resolution import handle_proposal_resolution

            await handle_proposal_resolution(
                rt.db, prop, status,
                reason=user_response or None,
                source="dashboard",
                memory_store=getattr(rt, "_memory_store", None),
                autonomy_manager=getattr(rt, "_autonomy_manager", None),
            )
        except Exception:
            logger.warning(
                "resolution hook failed for %s", proposal_id, exc_info=True,
            )

    # Trigger delayed sweep on approval — same 5-min grace as Telegram,
    # so the user can revoke before dispatch fires.
    if status == "approved" and rt.ego_session:
        try:
            from genesis.util.tasks import tracked_task

            async def _dashboard_delayed_sweep() -> None:
                await asyncio.sleep(300)  # 5-min grace period
                if rt.ego_session:
                    await rt.ego_session.sweep_approved_proposals()

            tracked_task(
                _dashboard_delayed_sweep(),
                name="dashboard_proposal_sweep",
            )
            logger.info(
                "Dashboard approval — sweep scheduled in 5 min for proposal %s",
                proposal_id,
            )
        except Exception:
            logger.warning(
                "Failed to schedule sweep after dashboard approval for %s",
                proposal_id,
                exc_info=True,
            )

    return jsonify({"id": proposal_id, "status": status})
