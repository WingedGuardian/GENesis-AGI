"""Dashboard routes for the WS-8 owner-visibility "Activity" tab (autonomy).

Read-only views of what Genesis is AUTHORIZED to do autonomously (GRANTED
capability cells) and what it has DONE under that authority (the
``autonomous_email_sends`` ledger), plus a flag-as-bad action that records a
correction on the send's cell (demoting a GRANTED cell back to ASK).  This is the
owner's pull-based visibility + control path — the per-send Telegram notification
is muted by default, so this tab is how the owner keeps tabs and corrects.

Every route is gated with ``is_authenticated()`` (a no-op when
``DASHBOARD_PASSWORD`` is unset, a 403 when it is set).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.dashboard.auth import is_authenticated

logger = logging.getLogger(__name__)


def _auth_or_403():
    """Return a 403 response tuple if not authenticated, else None."""
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 403
    return None


@blueprint.route("/api/genesis/autonomy/grants")
@_async_route
async def autonomy_grants():
    """Standing autonomy — the GRANTED capability cells (what Genesis may do
    without owner approval)."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import capability_grants as cg
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"grants": []})
    return jsonify({"grants": await cg.list_granted(rt.db)})


@blueprint.route("/api/genesis/autonomy/sends")
@_async_route
async def autonomy_sends():
    """The autonomous-send action log (most recent first)."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import autonomous_email_sends as aes
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"sends": []})
    limit = max(1, min(request.args.get("limit", 100, type=int), 500))
    return jsonify({"sends": await aes.list_recent(rt.db, limit=limit)})


@blueprint.route("/api/genesis/autonomy/sends/<send_id>/flag", methods=["POST"])
@_async_route
async def autonomy_flag_send(send_id: str):
    """Flag a sent autonomous email as bad → record a correction on its cell,
    demoting a GRANTED cell back to ASK.  Idempotent: a second flag on the same
    send is a no-op (never double-demotes)."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import autonomous_email_sends as aes
    from genesis.db.crud import capability_grants as cg
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    send = await aes.get_by_id(rt.db, send_id)
    if send is None:
        return jsonify({"error": "send not found"}), 404

    now = datetime.now(UTC).isoformat()
    newly_flagged = await aes.mark_flagged(rt.db, send_id, flagged_at=now)
    cell_state = None
    if newly_flagged:
        state = await cg.record_correction(
            rt.db,
            domain=send["cell_domain"], verb=send["cell_verb"],
            risk_class=send["cell_risk_class"], updated_at=now,
        )
        cell_state = state.value
        logger.info(
            "Owner flagged autonomous send %s — corrected cell %s:%s:%s (now %s)",
            send_id, send["cell_domain"], send["cell_verb"],
            send["cell_risk_class"], cell_state,
        )
    return jsonify({"flagged": newly_flagged, "cell_state": cell_state})
