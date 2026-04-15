"""Outreach messages and surplus detail routes."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/outreach/messages")
@_async_route
async def outreach_messages():
    """Return recent outreach messages with full content."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify([])

    rt.db.row_factory = aiosqlite.Row
    limit = request.args.get("limit", 20, type=int)
    cursor = await rt.db.execute(
        """SELECT id, category, signal_type, topic, channel, message_content,
                  delivered_at, engagement_outcome, user_response, created_at
           FROM outreach_history
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return jsonify([dict(r) for r in rows])


@blueprint.route("/api/genesis/outreach/<msg_id>/engage", methods=["POST"])
@_async_route
async def outreach_engage(msg_id: str):
    """Record user engagement with an outreach message."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    outcome = data.get("outcome", "engaged")
    response = data.get("response", "")

    now = datetime.now(UTC).isoformat()
    await rt.db.execute(
        """UPDATE outreach_history
           SET engagement_outcome = ?, user_response = ?, action_taken = ?
           WHERE id = ?""",
        (outcome, response, now, msg_id),
    )
    await rt.db.commit()
    return jsonify({"ok": True})


    # surplus_detail() moved to routes/surplus.py
