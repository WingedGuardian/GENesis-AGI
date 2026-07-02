"""outreach-mcp server — proactive messaging, engagement tracking, user preferences."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime

import httpx
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

        mcp.add_middleware(InstrumentationMiddleware(activity_tracker, "outreach", db=db))


@mcp.tool()
async def outreach_send(
    message: str,
    category: str,
    channel: str,
    urgency: str = "low",
    preferred_timing: str | None = None,
    salience_score: float = 0.5,
    labeled_surplus: bool = False,
    thread_id: str | None = None,
) -> str:
    """Queue a message for delivery. Returns outreach_id.

    For email replies, pass thread_id to route to the correct recipient.
    The thread_id maps to a registered email thread whose recipient is
    used for delivery.
    """
    # Resolve the per-thread recipient for email sends BEFORE the
    # pipeline/fallback split — so a QUEUED follow-up (pipeline=None subprocess)
    # carries its thread recipient through pending_outreach instead of arriving
    # recipient-less and self-sending to the agent's own address on drain.
    validated_recipient: str | None = None
    if thread_id and channel == "email" and _db is not None:
        from genesis.db.crud import email_threads
        thread = await email_threads.get_thread(_db, thread_id)
        if thread:
            validated_recipient = thread.get("recipient")
            logger.info(
                "Thread %s resolved recipient: %s", thread_id, validated_recipient,
            )
        else:
            logger.warning("Thread %s not found for recipient lookup", thread_id)
    elif channel == "email" and not thread_id:
        logger.warning(
            "outreach_send email without thread_id — recipient will come from "
            "OUTREACH_RECIPIENT_EMAIL if configured, else the send is dropped "
            "(IGNORED) by the pipeline self-send guard"
        )

    if not _pipeline:
        # Validate category before enqueuing (same check the pipeline path does)
        from genesis.outreach.types import OutreachCategory

        try:
            OutreachCategory(category)
        except ValueError:
            valid = ", ".join(c.value for c in OutreachCategory)
            return json.dumps({
                "error": f"Invalid category '{category}'. Valid categories: {valid}",
            })

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
                thread_id=thread_id,
                validated_recipient=validated_recipient,
            )
            return json.dumps({
                "status": "queued",
                "pending_id": pending_id,
                "deliver_after": preferred_timing,
            })
        return "Error: outreach pipeline not initialized and no DB available"
    from genesis.outreach.types import (
        OutreachCategory,
        OutreachRequest,
        OutreachStatus,
    )

    try:
        cat = OutreachCategory(category)
    except ValueError:
        return f"Error: invalid category '{category}'"

    # validated_recipient was resolved above (shared with the fallback path).
    req = OutreachRequest(
        category=cat,
        topic=message[:100],
        context=message,
        salience_score=salience_score,
        signal_type=category,
        channel=channel,
        labeled_surplus=labeled_surplus,
        validated_recipient=validated_recipient,
        thread_id=thread_id,
    )
    if urgency == "critical":
        result = await _pipeline.submit_urgent(req)
    else:
        result = await _pipeline.submit(req)
    if result.status == OutreachStatus.HELD:
        # WS-8 Tenet 0b: a neutral, TERMINAL outcome for the acting model — no
        # policy, no pending-obligation framing, no behavioral directive. The
        # real pending obligation lives system-side (pending_email_sends +
        # approval_requests → the owner's approval surface), invisible here.
        return json.dumps({
            "status": "not_performed",
            "reason": "owner_authorization",
            "message": (
                "This action is governed by Genesis owner authorization and was "
                "not performed. This is expected, routine behavior, not an error "
                "or failure, and not something to work around."
            ),
        })
    return json.dumps({
        "outreach_id": result.outreach_id,
        "status": result.status.value,
        "channel": result.channel,
        "error": result.error,
    })


@mcp.tool()
async def outreach_poll(
    channel: str,
    question: str,
    answers: list[str],
    duration_hours: int = 168,
    allow_multiselect: bool = False,
) -> str:
    """Create a Discord poll via webhook. Returns JSON with message_id.

    Args:
        channel: Webhook name (e.g. "announcements", "general", "dev-discussion").
        question: Poll question text (max 300 chars).
        answers: List of answer options (max 10, each max 55 chars).
        duration_hours: How long the poll stays open (default 7 days, max 768h).
        allow_multiselect: Whether users can vote for multiple options.
    """
    # Resolve webhook URL from environment
    env_key = f"DISCORD_WEBHOOK_{channel.upper().replace('-', '_')}"
    webhook_url = os.environ.get(env_key) or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return json.dumps({"error": f"No webhook URL found (tried {env_key} and DISCORD_WEBHOOK_URL)"})

    # ── Dedup check: skip if same poll posted within 7 days ──
    if _db is not None:
        from genesis.outreach.governance import content_hash

        chash = content_hash(question)
        try:
            cursor = await _db.execute(
                "SELECT COUNT(*) FROM outreach_history "
                "WHERE signal_type = 'discord_poll' AND content_hash = ? "
                "AND delivered_at IS NOT NULL "
                "AND delivered_at >= datetime('now', '-7 days')",
                (chash,),
            )
            row = await cursor.fetchone()
            if row and row[0] > 0:
                logger.info("Discord poll dedup: skipping duplicate (hash=%s)", chash[:12])
                return json.dumps({"status": "skipped", "reason": "duplicate_poll_within_7_days"})
        except Exception:
            logger.debug("Poll dedup check failed, proceeding", exc_info=True)

    # Egress gate (Discord = external audience): this path posts straight to the
    # webhook, bypassing OutreachPipeline._deliver, so scrub anti-slop + PII-scan
    # here too. Em-dash auto-fixed; quarantine if the question leaks secrets.
    from genesis.content.egress import gate as _egress_gate

    gated = [
        _egress_gate(t, channel="discord", category="content")
        for t in (question, *answers)
    ]
    for g in gated:
        if g.quarantined:
            return json.dumps(
                {"error": f"Poll content scan quarantine: {g.scan.detected}"}
            )
    question = gated[0].text
    answers = [g.text for g in gated[1:]]

    url = f"{webhook_url}?wait=true"
    payload = {
        "poll": {
            "question": {"text": question[:300]},
            "answers": [
                {"poll_media": {"text": a[:55]}} for a in answers[:10]
            ],
            "duration": min(duration_hours, 768),
            "allow_multiselect": allow_multiselect,
        }
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            msg_id = data.get("id", "")
        logger.info("Discord poll created via %s (msg_id=%s)", channel, msg_id)

        # WS5 Discord capability SHADOW-gate: observe (never hold) this poll AFTER it's
        # posted, so the post is never delayed by the shadow write. Best-effort, read-
        # only (guards a None _db in standalone mode). The import + call are wrapped so a
        # shadow/import failure can never flip an already-posted poll into an error return
        # (which the caller could otherwise retry → double-post).
        try:
            from genesis.autonomy.shadow_gate import observe_discord_send

            await observe_discord_send(
                _db, path="poll", verb="poll", risk_class="bulk",
                target=channel, content=question,
            )
        except Exception:  # noqa: BLE001 — shadow is best-effort; never break the poll
            logger.debug("outreach_poll capability shadow observe failed", exc_info=True)

        # ── Record to outreach_history for dedup + campaign visibility ──
        if _db is not None:
            from genesis.outreach.governance import content_hash as _ch

            now_iso = datetime.now(UTC).isoformat()
            outreach_id = str(uuid.uuid4())
            try:
                from genesis.db.crud import outreach as outreach_crud

                await outreach_crud.create(
                    _db,
                    id=outreach_id,
                    signal_type="discord_poll",
                    topic=question[:100],
                    category="content",
                    salience_score=0.5,
                    channel="discord",  # adapter name, not sub-channel
                    message_content=question,
                    created_at=now_iso,
                    delivery_id=msg_id,
                    content_hash=_ch(question),
                )
                await outreach_crud.record_delivery(
                    _db, outreach_id, delivered_at=now_iso,
                )
            except Exception:
                logger.warning("Failed to record poll in outreach_history", exc_info=True)

        return json.dumps({"status": "created", "message_id": msg_id, "channel": channel})
    except httpx.HTTPStatusError as exc:
        error_body = exc.response.text[:200] if exc.response else ""
        return json.dumps({"error": f"Discord API error {exc.response.status_code}: {error_body}"})
    except Exception as exc:
        return json.dumps({"error": f"Poll creation failed: {exc}"})


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
        ),
        channel_preferences={**current.channel_preferences, **preferences.get("channel_preferences", {})},
        thresholds={**current.thresholds, **preferences.get("thresholds", {})},
        max_daily=int(rl.get("max_daily", current.max_daily)),
        surplus_daily=int(rl.get("surplus_daily", current.surplus_daily)),
        content_daily=int(rl.get("content_daily", current.content_daily)),
        morning_report_time=mr.get("trigger_time", current.morning_report_time),
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
