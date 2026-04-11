"""Fire alarm classifier — maps health alerts to Sentinel response tiers.

Tier 1: Defense mechanism failures (guards are down) → CC immediately
Tier 2: Cascading / compounding failures → reflexes first, then CC
Tier 3: Persistent unresolved conditions → reflexes only unless exhausted

The classifier consumes health_alerts from the health MCP and categorizes
them by severity and pattern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Alert IDs that indicate defense mechanism failures (Tier 1)
_TIER1_PATTERNS = {
    "service:watchdog_blind",
    "service:genesis_down",
    # NOTE: cc:unavailable and cc:quota_exhausted are deliberately NOT here.
    # The Sentinel's only response tool is dispatching a CC session. If CC
    # is genuinely unavailable, a diagnostic CC session cannot run — waking
    # the tool to fix the tool it's missing is a self-defeating loop, and
    # each failed dispatch burns a per-pattern backoff slot for nothing.
    # Emission severity for cc:quota_exhausted is WARNING (see
    # mcp/health/errors.py), which routes to Tier 3 via the default path
    # — the alert stays visible in the dashboard and health_alerts MCP,
    # but Sentinel doesn't wake. See Part 9c in
    # .claude/plans/fluttering-humming-bentley.md for the incident.
    # Guardian is the host-side safety net. If its heartbeat stops updating,
    # the container has lost its external eyes on itself. This must wake
    # the Sentinel immediately regardless of other conditions.
    "guardian:heartbeat_stale",
}

# Alert IDs for cascading/compounding failures (Tier 2)
_TIER2_PATTERNS = {
    "memory:critical",
    "disk:critical",
    "tmpfs:critical",
    "embedding:unreachable",
    "qdrant:unreachable",
    "qdrant:collections_missing",
}


@dataclass(frozen=True)
class FireAlarm:
    """A classified fire alarm condition."""

    tier: int  # 1, 2, or 3
    alert_id: str
    severity: str
    message: str

    @property
    def is_defense_failure(self) -> bool:
        return self.tier == 1


def classify_alerts(alerts: list[dict]) -> list[FireAlarm]:
    """Classify health alerts into fire alarm tiers.

    Args:
        alerts: List of alert dicts from health_alerts MCP (each has
                id, severity, message).

    Returns:
        Fire alarms sorted by tier (worst first). Empty list if no alarms.
    """
    alarms: list[FireAlarm] = []

    for alert in alerts:
        alert_id = alert.get("id", "")
        severity = alert.get("severity", "").upper()
        message = alert.get("message", "")

        # Tier 1: Defense mechanism failures
        if alert_id in _TIER1_PATTERNS:
            alarms.append(FireAlarm(tier=1, alert_id=alert_id, severity=severity, message=message))
            continue

        # Tier 2: Critical infrastructure — known cascading patterns OR any
        # CRITICAL severity from a trusted emitter. Severity is the contract:
        # if mcp/health/errors.py emits something at CRITICAL, it MEANS
        # critical, and Sentinel should wake for it. Don't silence the
        # listener — fix dishonest emitters at the source instead.
        #
        # The call_site:* false-CRITICAL spam was fixed by source-gating in
        # mcp/health/errors.py (skip status=="disabled" + skip wired==False),
        # NOT by removing this blanket rule. Removing it silently dropped 8
        # legitimate infrastructure CRITICAL alerts (memory, disk, tmpfs,
        # qdrant, embeddings, awareness tick overdue, etc.) — see the
        # regression test test_all_emitted_critical_ids_classify below.
        if alert_id in _TIER2_PATTERNS or severity == "CRITICAL":
            alarms.append(FireAlarm(tier=2, alert_id=alert_id, severity=severity, message=message))
            continue

        # Tier 3: Any remaining WARNING alerts
        if severity == "WARNING":
            alarms.append(FireAlarm(tier=3, alert_id=alert_id, severity=severity, message=message))

    # Sort by tier (worst first), then by severity within tier
    alarms.sort(key=lambda a: (a.tier, 0 if a.severity == "CRITICAL" else 1))

    if alarms:
        tier_counts = {}
        for a in alarms:
            tier_counts[a.tier] = tier_counts.get(a.tier, 0) + 1
        logger.info("Fire alarm classification: %s", tier_counts)

    return alarms


def worst_tier(alarms: list[FireAlarm]) -> int | None:
    """Return the worst (lowest) tier from a list of alarms, or None if empty."""
    if not alarms:
        return None
    return min(a.tier for a in alarms)
