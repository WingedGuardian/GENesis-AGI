"""outreach-mcp server — proactive messaging, engagement tracking, user preferences."""

from __future__ import annotations

import json
import logging

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("genesis-outreach")

_pipeline = None
_engagement = None
_config = None
_db = None


def init_outreach_mcp(*, pipeline, engagement, config, db, activity_tracker=None) -> None:
    """Wire runtime dependencies. Called by GenesisRuntime."""
    global _pipeline, _engagement, _config, _db
    _pipeline = pipeline
    _engagement = engagement
    _config = config
    _db = db

    # Ensure pending_outreach table exists for standalone fallback
    if db is not None and pipeline is None:
        import asyncio
        import contextlib

        from genesis.db.crud.pending_outreach import ensure_table

        with contextlib.suppress(RuntimeError):
            asyncio.get_event_loop().create_task(ensure_table(db))

    if activity_tracker is not None:
        from genesis.observability.mcp_middleware import InstrumentationMiddleware

        mcp.add_middleware(InstrumentationMiddleware(activity_tracker, "outreach"))


@mcp.tool()
async def outreach_send(
    message: str,
    category: str,
    channel: str,
    urgency: str = "low",
    preferred_timing: str | None = None,
    salience_score: float = 0.5,
    labeled_surplus: bool = False,
) -> str:
    """Queue a message for delivery. Returns outreach_id."""
    if not _pipeline:
        # Queue for genesis-server to pick up on next cycle
        if _db is not None:
            from genesis.db.crud import pending_outreach

            await pending_outreach.ensure_table(_db)  # idempotent safety net
            pending_id = await pending_outreach.enqueue(
                _db,
                message=message,
                category=category,
                channel=channel,
                urgency=urgency,
                deliver_after=preferred_timing,
            )
            return json.dumps({
                "status": "queued",
                "pending_id": pending_id,
                "deliver_after": preferred_timing,
            })
        return "Error: outreach pipeline not initialized and no DB available"
    from genesis.outreach.types import OutreachCategory, OutreachRequest

    try:
        cat = OutreachCategory(category)
    except ValueError:
        return f"Error: invalid category '{category}'"

    req = OutreachRequest(
        category=cat,
        topic=message[:100],
        context=message,
        salience_score=salience_score,
        signal_type=category,
        channel=channel,
        labeled_surplus=labeled_surplus,
    )
    if urgency == "critical":
        result = await _pipeline.submit_urgent(req)
    else:
        result = await _pipeline.submit(req)
    return json.dumps({
        "outreach_id": result.outreach_id,
        "status": result.status.value,
        "channel": result.channel,
        "error": result.error,
    })


@mcp.tool()
async def outreach_queue(
    category: str | None = None,
    channel: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """View recent outreach messages."""
    if not _db:
        return [{"error": "not initialized"}]
    try:
        query = "SELECT id, category, channel, topic, delivered_at, engagement_outcome FROM outreach_history"
        conditions, params = [], []
        if category:
            conditions.append("category = ?")
            params.append(category)
        if channel:
            conditions.append("channel = ?")
            params.append(channel)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT 20"
        cursor = await _db.execute(query, params)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in await cursor.fetchall()]
    except Exception as exc:
        return [{"error": f"Query failed: {exc}"}]


@mcp.tool()
async def outreach_send_and_wait(
    message: str,
    category: str = "blocker",
    channel: str = "telegram",
    timeout_seconds: int = 300,
) -> str:
    """Send a message and wait for user reply. Returns JSON with reply or timeout."""
    if not _pipeline:
        return "Error: outreach pipeline not initialized"
    from genesis.outreach.types import OutreachCategory, OutreachRequest

    try:
        cat = OutreachCategory(category)
    except ValueError:
        return f"Error: invalid category '{category}'"

    req = OutreachRequest(
        category=cat,
        topic=message[:100],
        context=message,
        salience_score=1.0,
        signal_type=category,
        channel=channel,
    )
    result, reply = await _pipeline.submit_and_wait(req, timeout_s=float(timeout_seconds))
    return json.dumps({
        "outreach_id": result.outreach_id,
        "status": result.status.value,
        "reply": reply,
        "timed_out": reply is None and result.status.value == "delivered",
    })


@mcp.tool()
async def outreach_engagement(
    outreach_id: str,
    signal: str,
    channel: str | None = None,
) -> bool:
    """Record engagement event (delivered, opened, replied, acted_on, ignored)."""
    if not _db:
        return False
    from genesis.db.crud import outreach as crud
    await crud.record_engagement(_db, outreach_id, engagement_outcome=signal, engagement_signal=signal)
    return True


@mcp.tool()
async def outreach_preferences(
    action: str = "get",
    preferences: dict | None = None,
) -> dict:
    """Get/set user channel preferences and quiet hours."""
    global _config
    if action == "get":
        if _config:
            return {
                "channel_preferences": _config.channel_preferences,
                "quiet_hours": {
                    "start": _config.quiet_hours.start,
                    "end": _config.quiet_hours.end,
                    "timezone": _config.quiet_hours.timezone,
                },
                "thresholds": _config.thresholds,
                "rate_limits": {
                    "max_daily": _config.max_daily,
                    "surplus_daily": _config.surplus_daily,
                },
            }
        # Standalone mode — use config loader which has sensible defaults
        from genesis.outreach.config import load_outreach_config

        cfg = load_outreach_config()
        return {
            "channel_preferences": cfg.channel_preferences,
            "quiet_hours": {
                "start": cfg.quiet_hours.start,
                "end": cfg.quiet_hours.end,
                "timezone": cfg.quiet_hours.timezone,
            },
            "thresholds": cfg.thresholds,
            "rate_limits": {
                "max_daily": cfg.max_daily,
                "surplus_daily": cfg.surplus_daily,
            },
            "source": "defaults (standalone mode)",
        }
    if not preferences or not isinstance(preferences, dict):
        return {"error": "preferences must be a non-empty dict"}

    from genesis.outreach.config import (
        load_outreach_config,
        save_outreach_config,
        validate_preferences,
    )

    errors = validate_preferences(preferences)
    if errors:
        return {"error": "validation failed", "validation_errors": errors}

    # Load current config, merge incoming preferences
    current = _config or load_outreach_config()

    # Build merged values
    qh = preferences.get("quiet_hours", {})
    rl = preferences.get("rate_limits", {})
    mr = preferences.get("morning_report", {})
    eng = preferences.get("engagement", {})

    from genesis.outreach.config import OutreachConfig, QuietHours

    merged = OutreachConfig(
        quiet_hours=QuietHours(
            start=qh.get("start", current.quiet_hours.start),
            end=qh.get("end", current.quiet_hours.end),
            timezone=qh.get("timezone", current.quiet_hours.timezone),
        ),
        channel_preferences={**current.channel_preferences, **preferences.get("channel_preferences", {})},
        thresholds={**current.thresholds, **preferences.get("thresholds", {})},
        max_daily=int(rl.get("max_daily", current.max_daily)),
        surplus_daily=int(rl.get("surplus_daily", current.surplus_daily)),
        morning_report_time=mr.get("trigger_time", current.morning_report_time),
        morning_report_timezone=mr.get("timezone", current.morning_report_timezone),
        engagement_timeout_hours=int(eng.get("timeout_hours", current.engagement_timeout_hours)),
        engagement_poll_minutes=int(eng.get("poll_interval_minutes", current.engagement_poll_minutes)),
        immediate_escalation_alerts=current.immediate_escalation_alerts,
    )

    try:
        save_outreach_config(merged)
    except Exception as exc:
        logger.error("Failed to save outreach config: %s", exc, exc_info=True)
        return {"error": f"failed to save config: {exc}"}

    # Hot-reload: update module-level config and pipeline references
    _config = merged
    if _pipeline:
        _pipeline.reload_config(merged)

    return {"status": "ok", "applied": preferences}


@mcp.tool()
async def outreach_digest(
    period: str = "daily",
    category_filter: list[str] | None = None,
) -> dict:
    """Generate a digest of recent outreach activity."""
    if not _db:
        return {"error": "not initialized"}
    try:
        interval = "-1 day" if period == "daily" else "-7 days"
        cursor = await _db.execute(
            "SELECT category, engagement_outcome, COUNT(*) FROM outreach_history "
            "WHERE delivered_at >= datetime('now', ?) "
            "GROUP BY category, engagement_outcome",
            (interval,),
        )
        rows = await cursor.fetchall()
        summary = {}
        for cat, outcome, count in rows:
            if category_filter and cat not in category_filter:
                continue
            summary.setdefault(cat, {})[outcome or "pending"] = count
        return {"period": period, "summary": summary}
    except Exception as exc:
        return {"error": f"Digest query failed: {exc}"}
