"""Flask blueprint for outreach dashboard API."""

from __future__ import annotations

import logging
from functools import wraps

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

outreach_api = Blueprint("outreach_api", __name__, url_prefix="/api/genesis/outreach")

_db = None
_pipeline = None
_config = None


def init_outreach_api(*, db, pipeline=None, config=None) -> None:
    global _db, _pipeline, _config
    _db = db
    _pipeline = pipeline
    _config = config


def _async_route(f):
    import asyncio

    @wraps(f)
    def wrapper(*args, **kwargs):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        finally:
            loop.close()

    return wrapper


@outreach_api.route("/queue")
@_async_route
async def get_queue():
    if not _db:
        return jsonify({"error": "not initialized"}), 503
    limit = request.args.get("limit", 20, type=int)
    cursor = await _db.execute(
        "SELECT id, category, signal_type, topic, channel, delivered_at, "
        "engagement_outcome, created_at FROM outreach_history "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r, strict=True)) for r in await cursor.fetchall()]
    return jsonify(rows)


@outreach_api.route("/engagement")
@_async_route
async def get_engagement():
    if not _db:
        return jsonify({"error": "not initialized"}), 503
    cursor = await _db.execute(
        "SELECT engagement_outcome, COUNT(*) as count FROM outreach_history "
        "WHERE delivered_at >= datetime('now', '-7 days') "
        "AND engagement_outcome IS NOT NULL "
        "GROUP BY engagement_outcome"
    )
    rows = await cursor.fetchall()
    summary = {r[0]: r[1] for r in rows}
    total = sum(summary.values())
    return jsonify({
        "period": "7d",
        "total": total,
        "breakdown": summary,
        "engagement_rate": summary.get("engaged", 0) / total if total else 0,
    })


@outreach_api.route("/surplus")
@_async_route
async def get_surplus():
    if not _db:
        return jsonify({"error": "not initialized"}), 503
    cursor = await _db.execute(
        "SELECT id, content, confidence, drive_alignment, created_at, ttl "
        "FROM surplus_insights WHERE promotion_status = 'pending' "
        "AND ttl > datetime('now') ORDER BY confidence DESC LIMIT 20"
    )
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r, strict=True)) for r in await cursor.fetchall()]
    return jsonify(rows)


@outreach_api.route("/surplus/<insight_id>/approve", methods=["POST"])
@_async_route
async def approve_surplus(insight_id):
    if not _db:
        return jsonify({"error": "not initialized"}), 503
    await _db.execute(
        "UPDATE surplus_insights SET promotion_status = 'promoted' WHERE id = ?",
        (insight_id,),
    )
    await _db.commit()
    return jsonify({"status": "approved", "id": insight_id})


@outreach_api.route("/surplus/<insight_id>/reject", methods=["POST"])
@_async_route
async def reject_surplus(insight_id):
    if not _db:
        return jsonify({"error": "not initialized"}), 503
    await _db.execute(
        "UPDATE surplus_insights SET promotion_status = 'discarded' WHERE id = ?",
        (insight_id,),
    )
    await _db.commit()
    return jsonify({"status": "rejected", "id": insight_id})


@outreach_api.route("/config")
@_async_route
async def get_config():
    if not _config:
        return jsonify({"error": "not initialized"}), 503
    return jsonify({
        "quiet_hours": {
            "start": _config.quiet_hours.start,
            "end": _config.quiet_hours.end,
            "timezone": _config.quiet_hours.timezone,
        },
        "channel_preferences": _config.channel_preferences,
        "thresholds": _config.thresholds,
        "rate_limits": {"max_daily": _config.max_daily, "surplus_daily": _config.surplus_daily},
        "morning_report": {
            "time": _config.morning_report_time,
            "timezone": _config.morning_report_timezone,
        },
    })
