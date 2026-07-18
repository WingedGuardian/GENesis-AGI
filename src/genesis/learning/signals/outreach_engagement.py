"""Real OutreachEngagementCollector — replaces stub in awareness/signals.py."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import SignalReading
from genesis.outreach.types import POSITIVE_ENGAGEMENT_OUTCOMES


class OutreachEngagementCollector:
    """Unified engagement ratio over ALL Genesis outbound, last 7 days.

    Two outbound surfaces feed one ratio:

    - outreach_history rows with an engagement_outcome (existing behavior;
      positive set from genesis.outreach.types), and
    - ego_proposals created in the window — each proposal counts toward
      total, and a resolution carrying a typed user_response (approve or
      reject WITH words) counts as engagement. A typed deny reason is the
      strongest engagement signal the user can give; before this fold it
      counted as zero and the system reported "user doesn't engage" while
      the user was actively ruling on proposals.
    """

    signal_name = "outreach_engagement_data"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        note = (
            "Unified engagement over Genesis outbound (outreach messages + "
            "ego proposals), last 7 days. Engaged = positive outreach "
            "outcomes + proposal resolutions with a typed reason. "
            "0.0=no outbound or no engagement"
        )
        cursor = await self._db.execute(
            "SELECT engagement_outcome, COUNT(*) FROM outreach_history "
            "WHERE delivered_at >= datetime('now', '-7 days') "
            "AND engagement_outcome IS NOT NULL "
            "GROUP BY engagement_outcome"
        )
        rows = await cursor.fetchall()
        total = sum(r[1] for r in rows)
        # Was `r[0] == "engaged"` alone, so a real reply ('useful') never counted
        # and this ratio was ~0.0 regardless of true engagement. Use the canonical
        # positive set (see genesis.outreach.types).
        engaged = sum(r[1] for r in rows if r[0] in POSITIVE_ENGAGEMENT_OUTCOMES)

        # Ego proposals: ego_proposals columns only (proposal digests write
        # NO outreach_history rows — verified 2026-07-18).
        cursor = await self._db.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN user_response IS NOT NULL "
            "AND TRIM(user_response) != '' THEN 1 ELSE 0 END) "
            "FROM ego_proposals "
            "WHERE created_at >= datetime('now', '-7 days')"
        )
        prow = await cursor.fetchone()
        total += int(prow[0] or 0)
        engaged += int(prow[1] or 0)

        value = engaged / total if total > 0 else 0.0
        return SignalReading(
            name=self.signal_name,
            value=round(value, 3),
            source="outreach_history+ego_proposals",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note=note,
        )
