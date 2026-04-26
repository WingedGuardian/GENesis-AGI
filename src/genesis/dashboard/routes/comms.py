"""Unified Communications route — aggregates outreach, ego proposals, and approvals."""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


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
    try:
        from genesis.db.crud import ego

        if view == "pending":
            proposals = await ego.list_pending_proposals(rt.db)
        else:
            proposals = await ego.list_proposals(rt.db, limit=limit)

        pending_count_cursor = await rt.db.execute(
            "SELECT COUNT(*) FROM ego_proposals WHERE status = 'pending'"
        )
        counts["proposals_pending"] = (await pending_count_cursor.fetchone())[0]

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

    return jsonify({"id": proposal_id, "status": status})
