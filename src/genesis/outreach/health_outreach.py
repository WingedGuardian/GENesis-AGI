"""Health outreach — surfaces critical infrastructure problems to the user.

Only alerts in the immediate_escalation whitelist with CRITICAL severity
reach Telegram. Everything else stays internal (dashboard, morning report,
awareness signals).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.outreach.types import OutreachCategory, OutreachRequest

logger = logging.getLogger(__name__)

# Don't re-send the same alert within this window
_DEDUP_HOURS = 6


class HealthOutreachBridge:
    """Bridges health alerts → outreach requests.

    Called periodically by OutreachScheduler. Queries active alerts,
    filters to the immediate-escalation whitelist (CRITICAL only),
    deduplicates against recent outreach history, and yields outreach
    requests for genuinely user-actionable emergencies.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        escalation_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._db = db
        self._escalation_ids = escalation_ids

    async def check_and_generate(self) -> list[OutreachRequest]:
        """Check health alerts and generate outreach for immediate-escalation issues only."""
        from genesis.mcp.health_mcp import _impl_health_alerts

        alerts = await _impl_health_alerts(active_only=True)
        if not alerts:
            return []

        # Filter to CRITICAL + in the escalation whitelist
        immediate_alerts = [
            a for a in alerts
            if a.get("severity", "").upper() == "CRITICAL"
            and a.get("id", "") in self._escalation_ids
        ]

        suppressed = len(alerts) - len(immediate_alerts)
        if suppressed:
            logger.info(
                "Health outreach: %d alert(s) suppressed (internal-only), "
                "%d immediate-escalation candidate(s)",
                suppressed, len(immediate_alerts),
            )

        if not immediate_alerts:
            return []

        # Check dedup — which alerts have we already sent recently?
        cutoff = (datetime.now(UTC) - timedelta(hours=_DEDUP_HOURS)).isoformat()
        recently_sent = await self._get_recently_sent_alert_ids(cutoff)

        requests: list[OutreachRequest] = []
        for alert in immediate_alerts:
            alert_id = alert.get("id", "")
            if alert_id in recently_sent:
                continue

            message = alert.get("message", "Unknown issue")

            requests.append(OutreachRequest(
                category=OutreachCategory.BLOCKER,
                topic=f"Infrastructure Alert: {alert_id}",
                context=message,
                salience_score=1.0,
                signal_type="health_alert",
                source_id=alert_id,
            ))

        if requests:
            logger.info(
                "Health outreach: %d critical alert(s) to send (%d suppressed by dedup)",
                len(requests), len(immediate_alerts) - len(requests),
            )

        return requests

    async def _get_recently_sent_alert_ids(self, since_iso: str) -> set[str]:
        """Get alert IDs that were already sent as outreach recently.

        Uses topic field for dedup since outreach_history has no source_id
        column.  Topic is set to "Infrastructure Alert: {alert_id}".
        """
        try:
            cursor = await self._db.execute(
                "SELECT topic FROM outreach_history "
                "WHERE signal_type = 'health_alert' "
                "AND delivered_at IS NOT NULL "
                "AND delivered_at >= ?",
                (since_iso,),
            )
            rows = await cursor.fetchall()
            # Extract alert_id from "Infrastructure Alert: {alert_id}"
            ids: set[str] = set()
            for (topic,) in rows:
                if topic and topic.startswith("Infrastructure Alert: "):
                    ids.add(topic.removeprefix("Infrastructure Alert: "))
            return ids
        except Exception:
            logger.warning("Failed to query outreach dedup", exc_info=True)
            return set()
