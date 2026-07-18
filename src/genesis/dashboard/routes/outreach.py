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

    # WS-2 P1b: the outreach_history CHECK now enforces the vocabulary —
    # validate client input here so a bogus value 400s instead of 500ing
    # on IntegrityError.
    from genesis.outreach.types import ENGAGEMENT_OUTCOME_ALIASES, ENGAGEMENT_OUTCOMES

    outcome = ENGAGEMENT_OUTCOME_ALIASES.get(outcome, outcome)
    if outcome not in ENGAGEMENT_OUTCOMES:
        return jsonify(
            {"error": f"invalid outcome {outcome!r}", "allowed": sorted(ENGAGEMENT_OUTCOMES)}
        ), 400

    now = datetime.now(UTC).isoformat()
    await rt.db.execute(
        """UPDATE outreach_history
           SET engagement_outcome = ?, user_response = ?, action_taken = ?
           WHERE id = ?""",
        (outcome, response, now, msg_id),
    )
    await rt.db.commit()
    return jsonify({"ok": True})


@blueprint.route("/api/genesis/outreach/send_and_wait", methods=["POST"])
@_async_route
async def outreach_send_and_wait_rpc():
    """MCP RPC shim: send a message to the owner and block for their reply.

    The standalone MCP subprocess has no live pipeline, so `outreach_send_and_wait`
    there POSTs here; `@_async_route` runs this on the runtime loop that owns the
    pipeline + the single-owner Telegram reply-waiter. Deliberately a plain
    (LAN-reachable via the incus proxy) `/api/*` route — a trusted-LAN send
    primitive, consistent with the rest of the dashboard API; no extra auth.
    """
    from genesis.outreach.rpc import send_and_wait_via_pipeline
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.outreach_pipeline is None:
        return jsonify({"error": "outreach pipeline not ready"}), 503

    data = request.get_json(silent=True) or {}
    result = await send_and_wait_via_pipeline(
        rt.outreach_pipeline,
        message=data.get("message", ""),
        category=data.get("category", "blocker"),
        channel=data.get("channel", "telegram"),
        timeout_s=float(data.get("timeout_seconds", 1800)),
    )
    return jsonify(result)
