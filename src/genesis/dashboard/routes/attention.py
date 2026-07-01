"""Attention-engine shadow-review routes — the PR2 calibration cockpit.

Live, access-controlled view of ``attention_events`` (the offline shadow gate's decisions):
list + per-trigger stats (value-free, refs only), an explicit reveal of the window's
transcript text (resolved from the TRANSIENT snapshot, never genesis.db), and a
should/shouldn't/skip label write-back that feeds PR3's retune.

Security model (mirrors ``references.py``):
- Every route is gated with ``is_authenticated()`` (a NO-OP when ``DASHBOARD_PASSWORD`` is
  unset, enforced 403 when set) — the same deliberate, narrow exception to the dashboard's
  "API routes bypass auth" default. ``reveal-text`` returns ambient household conversation
  text, so it gets the same protection as references' secret reveal; the lockdown lever is
  setting ``DASHBOARD_PASSWORD`` (which gates the whole UI). list/stats are value-free.
- The firewall holds: list/stats never open a snapshot; only ``reveal-text`` reads the
  transient snapshot, and it persists nothing.
"""

from __future__ import annotations

import json
import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.dashboard.auth import is_authenticated

logger = logging.getLogger(__name__)


def _auth_or_403():
    """Return a 403 response tuple if not authenticated, else None."""
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 403
    return None


def _loads(raw, default):
    """Best-effort JSON parse for a stored column; ``default`` on empty/corrupt."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _event_summary(row: dict) -> dict:
    """Value-free event dict (refs + features + label) with the JSON columns parsed.

    ``window_ref`` carries ids + ts range (snapshot_id, utt_ids) — a reference, never text."""
    return {
        "id": row.get("id"),
        "ts": row.get("ts"),
        "session_id": row.get("session_id"),
        "activation": row.get("activation"),
        "score": row.get("score"),
        "clarity": row.get("clarity"),
        "mode_state": row.get("mode_state"),
        "triggers_fired": _loads(row.get("triggers_fired"), []),
        "suppressors": _loads(row.get("suppressors"), []),
        "window_ref": _loads(row.get("window_ref"), {}),
        "l15_verdict": _loads(row.get("l15_verdict"), None),
        "acceptance_signal": row.get("acceptance_signal"),
        "snapshot_id": row.get("snapshot_id"),
        "config_version": row.get("config_version"),
        "created_at": row.get("created_at"),
    }


@blueprint.route("/api/genesis/attention/list")
@_async_route
async def attention_list():
    """List shadow events (newest first), filtered. Never returns transcript text."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import attention as attention_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    activation = request.args.get("activation") or None
    trigger = request.args.get("trigger") or None
    is_user = request.args.get("is_user", "").lower() == "true"
    unlabeled = request.args.get("unlabeled", "").lower() == "true"
    limit = max(1, min(request.args.get("limit", 200, type=int), 500))
    offset = max(0, request.args.get("offset", 0, type=int))

    try:
        rows = await attention_crud.list_events(
            rt.db, activation=activation, trigger=trigger, is_user=is_user,
            unlabeled=unlabeled, limit=limit, offset=offset,
        )
        events = [_event_summary(r) for r in rows]
        return jsonify({"events": events, "count": len(events)})
    except Exception:
        logger.exception("Attention list failed")
        return jsonify({"error": "Failed to list attention events"}), 500


@blueprint.route("/api/genesis/attention/stats")
@_async_route
async def attention_stats():
    """Aggregate counts for the cockpit panel: labels, activation, per-trigger, suppressors."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import attention as attention_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        return jsonify({
            "labels": await attention_crud.label_counts(rt.db),
            "by_activation": await attention_crud.activation_stats(rt.db),
            "by_trigger": await attention_crud.trigger_stats(rt.db),
            "by_suppressor": await attention_crud.suppressor_stats(rt.db),
        })
    except Exception:
        logger.exception("Attention stats failed")
        return jsonify({"error": "Failed to fetch attention stats"}), 500


@blueprint.route("/api/genesis/attention/<event_id>/reveal-text", methods=["POST"])
@_async_route
async def attention_reveal_text(event_id: str):
    """Reveal the window's transcript text from the transient snapshot. Auth-gated.

    410 when the snapshot has been purged (the event is then review-read-only)."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.attention import sources
    from genesis.db.crud import attention as attention_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        row = await attention_crud.get_event(rt.db, event_id)
        if row is None:
            return jsonify({"error": "Event not found"}), 404
        wr = _loads(row.get("window_ref"), {})
        snapshot_id = wr.get("snapshot_id")
        utt_ids = wr.get("utt_ids") or []
        if not snapshot_id:
            return jsonify({"error": "Event has no resolvable window_ref"}), 422
        window = await sources.resolve_window_text(snapshot_id, utt_ids)
        if window is None:
            return jsonify({"error": "snapshot unavailable (purged)"}), 410
        return jsonify({"id": event_id, "window": window})
    except Exception:
        logger.exception("Attention reveal-text failed")
        return jsonify({"error": "Reveal failed"}), 500


@blueprint.route("/api/genesis/attention/<event_id>/label", methods=["POST"])
@_async_route
async def attention_label(event_id: str):
    """Write a should/shouldn't/skip review label. Returns the prior value (for X->Y)."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import attention as attention_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    signal = data.get("acceptance_signal")
    try:
        found, prior = await attention_crud.update_acceptance_signal(rt.db, event_id, signal)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Attention label failed")
        return jsonify({"error": "Label failed"}), 500

    if not found:
        return jsonify({"error": "Event not found"}), 404
    return jsonify({"status": "ok", "id": event_id, "acceptance_signal": signal, "prior": prior})
