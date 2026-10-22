"""Peer readiness transitions; awareness owns persistence and notification retries."""

from __future__ import annotations

from datetime import UTC, datetime

from genesis.db.crud import alert_events, outreach
from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

ALERT_ID = "agent_api:readiness"
SIGNAL = "peer_api_health"
_parked_pipeline = None


def alert_observation(value, *, boot_at=None, now=None):
    """Grace suppresses new incidents and cannot clear an existing incident."""
    if boot_at is not None and ((now or datetime.now(UTC)) - boot_at).total_seconds() < 90:
        return {"status": "unknown", "reasons": ["startup_grace"]}
    return value if isinstance(value, dict) else {"status": "unknown"}


async def active_alerts(db, observation):
    status = observation.get("status", "unknown")
    if status == "degraded":
        reasons = ", ".join(observation.get("reasons", []))
        return [{"id": ALERT_ID, "severity": "WARNING",
                 "message": f"Peer API not ready: {reasons}; pid={observation.get('pid')}; "
                            f"checked_at={observation.get('checked_at')}"}]
    if status in {"healthy", "disabled"} or db is None:
        return []
    return [{"id": row["alert_id"], "severity": row["severity"], "message": row["message"]}
            for row in await alert_events.list_open(db) if row["alert_id"] == ALERT_ID]


async def notify_pending(db, pipeline):
    """Incident + phase is the durable identity; no second delivery queue."""
    global _parked_pipeline
    if pipeline is None or pipeline is _parked_pipeline or not pipeline.channel_configured("telegram"):
        return
    rows = await (await db.execute(
        "SELECT * FROM alert_events WHERE alert_id=? ORDER BY created_at,id", (ALERT_ID,),
    )).fetchall()
    for row in rows:
        if row["resolved_at"] and row["resolution_reason"] != "healthy":
            continue
        phases = ["outage", "recovery"] if row["resolved_at"] else ["outage"]
        for phase in phases:
            topic = f"peer-api:{row['id']}:{phase}"
            if await outreach.delivered_topic_exists(db, signal_type=SIGNAL, topic=topic):
                continue
            if phase == "outage":
                text = f"Peer API outage observed at {row['created_at']}. {row['message']}"
            else:
                text = f"Peer API recovered at {row['resolved_at']} (incident began {row['created_at']})."
            result = await pipeline.submit_raw(text, OutreachRequest(
                category=OutreachCategory.ALERT, topic=topic, context=text,
                salience_score=1.0, signal_type=SIGNAL, channel="telegram",
                source_id=row["id"], defer_retry=False,
            ), best_effort=True)
            if result.status == OutreachStatus.IGNORED:
                _parked_pipeline = pipeline
                return
            if result.status != OutreachStatus.DELIVERED:
                # Stop here so recovery never overtakes its outage notice.
                return
