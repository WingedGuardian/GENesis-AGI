"""Deterministic governance gate — no LLM, purely config-driven checks."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import aiosqlite

from genesis.outreach.config import OutreachConfig
from genesis.outreach.types import (
    GovernanceResult,
    GovernanceVerdict,
    OutreachCategory,
    OutreachRequest,
)

logger = logging.getLogger(__name__)

_BYPASS_CATEGORIES = frozenset({OutreachCategory.BLOCKER, OutreachCategory.ALERT})
_MORNING_REPORT_SIGNAL = "morning_report"

# Per-signal_type dedup windows (hours).  Prevents awareness-loop retries from
# double-sending.  Keys not listed here fall through to _DEFAULT_DEDUP_HOURS.
# Three-tier policy:
#   - Informational alerts (12-24h): suppress duplicates entirely
#   - Approval requests (4h): re-send as reminder after window
#   - Escalations (8h): re-send as reminder, less often
_DEDUP_WINDOWS: dict[str, int] = {
    "health_alert": 12,
    "sentinel_approval": 4,
    "sentinel_escalation": 8,
    "sentinel_action_approval": 4,
    "autonomous_cli_fallback": 4,
    "genesis_update": 24,
    "code_audit": 6,
    "cc_version_update": 24,
    "morning_report": 24,
    "surplus_insight": 24,
    "surplus_opportunity": 24,
}
_DEFAULT_DEDUP_HOURS = 24


def content_hash(text: str) -> str:
    """SHA-256 of the first 200 characters — fuzzy content dedup key."""
    return hashlib.sha256(text[:200].encode()).hexdigest()


class GovernanceGate:
    """Deterministic pre-send governance. All checks are non-LLM."""

    def __init__(self, config: OutreachConfig, db: aiosqlite.Connection) -> None:
        self._config = config
        self._db = db

    async def check(self, request: OutreachRequest) -> GovernanceResult:
        if request.category in _BYPASS_CATEGORIES:
            # BLOCKER/ALERT bypass salience, quiet hours, engagement throttle
            # but NOT dedup — repeated identical alerts add noise, not
            # information.  The user who got "Ollama down" 2h ago knows it's
            # still down; re-sending doesn't help.  Approval requests get
            # shorter dedup windows (4h) so they act as reminders.
            if await self._is_duplicate(request):
                return GovernanceResult(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"{request.category.value} suppressed (duplicate within window)",
                    checks_failed=["dedup"],
                )
            return GovernanceResult(
                verdict=GovernanceVerdict.BYPASS,
                reason=f"{request.category.value} bypasses governance (dedup passed)",
                checks_passed=["category_bypass", "dedup"],
            )

        if request.signal_type == _MORNING_REPORT_SIGNAL:
            if await self._is_duplicate(request):
                return GovernanceResult(
                    verdict=GovernanceVerdict.DENY,
                    reason="morning report already sent today",
                    checks_failed=["dedup"],
                )
            return GovernanceResult(
                verdict=GovernanceVerdict.ALLOW,
                reason="morning report allowed",
                checks_passed=["morning_report_dedup"],
            )

        passed: list[str] = []
        failed: list[str] = []

        threshold = self._config.thresholds.get(request.category.value, 0.5)
        if request.salience_score >= threshold:
            passed.append("salience")
        else:
            failed.append(f"salience: {request.salience_score:.2f} < {threshold:.2f}")

        if self._in_quiet_hours():
            failed.append("quiet_hours: currently in quiet period")
        else:
            passed.append("quiet_hours")

        if await self._is_duplicate(request):
            failed.append("dedup: similar outreach sent recently")
        else:
            passed.append("dedup")

        if await self._within_rate_limit():
            passed.append("rate_limit")
        else:
            failed.append(f"rate_limit: exceeded {self._config.max_daily}/day")

        if request.category == OutreachCategory.SURPLUS:
            if await self._surplus_available():
                passed.append("surplus_quota")
            else:
                failed.append("surplus_quota: daily surplus already sent")

        # Engagement throttle: reduce outreach if ignore rate is high
        # BLOCKER/ALERT categories always exempt
        if request.category not in (OutreachCategory.BLOCKER, OutreachCategory.ALERT):
            throttle = await self._engagement_throttle(request)
            if throttle:
                failed.append(throttle)
            else:
                passed.append("engagement_throttle")

        if failed:
            return GovernanceResult(
                verdict=GovernanceVerdict.DENY,
                reason="; ".join(failed),
                checks_passed=passed,
                checks_failed=failed,
            )
        return GovernanceResult(
            verdict=GovernanceVerdict.ALLOW,
            reason="all checks passed",
            checks_passed=passed,
        )

    def _in_quiet_hours(self) -> bool:
        try:
            tz = ZoneInfo(self._config.quiet_hours.timezone)
        except Exception:
            tz = UTC
        now = datetime.now(tz).time()
        start = time.fromisoformat(self._config.quiet_hours.start)
        end = time.fromisoformat(self._config.quiet_hours.end)
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    async def is_duplicate(self, request: OutreachRequest) -> bool:
        """Check if a similar outreach was sent within the dedup window.

        Public API — used by submit_raw() for lightweight dedup on urgent paths.
        """
        return await self._is_duplicate(request)

    async def _is_duplicate(self, request: OutreachRequest) -> bool:
        window_hours = _DEDUP_WINDOWS.get(
            request.signal_type, _DEFAULT_DEDUP_HOURS
        )
        window_spec = f"-{window_hours} hours"

        # Primary key: (signal_type, topic, category) within the window
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM outreach_history "
            "WHERE signal_type = ? AND topic = ? AND category = ? "
            "AND delivered_at IS NOT NULL "
            "AND delivered_at >= datetime('now', ?)",
            (request.signal_type, request.topic, request.category.value, window_spec),
        )
        row = await cursor.fetchone()
        if (row[0] if row else 0) > 0:
            logger.info(
                "Duplicate suppressed: signal_type=%s topic=%r (exact match within %dh)",
                request.signal_type,
                request.topic,
                window_hours,
            )
            return True

        # Secondary: content-hash match (catches near-duplicate topics with same body)
        chash = content_hash(request.context)
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM outreach_history "
            "WHERE signal_type = ? AND category = ? AND content_hash = ? "
            "AND delivered_at IS NOT NULL "
            "AND delivered_at >= datetime('now', ?)",
            (request.signal_type, request.category.value, chash, window_spec),
        )
        row = await cursor.fetchone()
        if (row[0] if row else 0) > 0:
            logger.info(
                "Duplicate suppressed: signal_type=%s topic=%r (content hash match within %dh)",
                request.signal_type,
                request.topic,
                window_hours,
            )
            return True

        return False

    async def _within_rate_limit(self) -> bool:
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM outreach_history "
            "WHERE delivered_at IS NOT NULL "
            "AND category NOT IN ('blocker', 'alert') "
            "AND delivered_at >= date('now')",
        )
        row = await cursor.fetchone()
        return (row[0] if row else 0) < self._config.max_daily

    async def _surplus_available(self) -> bool:
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM outreach_history "
            "WHERE category = 'surplus' AND delivered_at IS NOT NULL "
            "AND delivered_at >= date('now')",
        )
        row = await cursor.fetchone()
        return (row[0] if row else 0) < self._config.surplus_daily

    async def _engagement_throttle(self, request) -> str | None:
        """Check if low engagement rate should throttle outreach.

        Returns an error string if throttled, None if allowed.
        Thresholds: >80% ignore with >=5 samples → deny surplus/digest.
        """
        try:
            from genesis.db.crud.outreach import get_engagement_stats
            stats = await get_engagement_stats(self._db, days=7)
        except Exception:
            return None  # Can't check → don't throttle

        total = stats["total"]
        if total < 5:
            return None  # Not enough data to throttle

        ignored = stats["ignored"]
        ignore_rate = ignored / total

        if ignore_rate > 0.9 and request.category.value in ("surplus", "digest"):
            return f"engagement_throttle: {ignore_rate:.0%} ignore rate (>{total} messages) — surplus blocked"

        if ignore_rate > 0.8:
            # Double the salience threshold for non-critical categories
            threshold = self._config.thresholds.get(request.category.value, 0.5)
            boosted = min(threshold * 2, 1.0)
            if request.salience_score < boosted:
                return (
                    f"engagement_throttle: {ignore_rate:.0%} ignore rate — "
                    f"salience {request.salience_score:.2f} below boosted threshold {boosted:.2f}"
                )

        return None
