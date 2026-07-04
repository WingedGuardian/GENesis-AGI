"""Fire alarm classifier — maps health alerts to Sentinel response tiers.

The Sentinel is Genesis's emergency responder — the firefighter. It is woken
only for fires it can actually put out: short-term emergencies, inside the
container, with a real remediation path. Classification is two-stage:

1. **Scope** (can we act?): an alert becomes a fire alarm only if it maps to
   an available remediation tool (see ``sentinel/remediation_map.py``).
   Unmapped or unavailable → never a FireAlarm, regardless of severity —
   the alert stays fully visible to the user/ego via the dashboard, health
   digest, and outreach escalation; it just doesn't wake the firefighter.
   Fail-closed. (Direct escalations via ``escalate_direct`` bypass this —
   the caller has already exercised judgment.)
2. **Tier** (how urgent?): unchanged semantics for alerts that pass scope —
   Tier 1: Defense mechanism failures (guards are down) → CC immediately
   Tier 2: Cascading / compounding failures → reflexes first, then CC
   Tier 3: Persistent unresolved conditions → reflexes only unless exhausted

The classifier consumes health_alerts from the health MCP and categorizes
them by severity and pattern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from genesis.sentinel.remediation_map import is_remediable

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
    # (They are also unmapped in capabilities.py, so the scope stage drops
    # them before tiering is ever considered — same rationale, enforced by
    # the same mechanism as every other non-remediable alert.)
    # Guardian is the host-side safety net. If its heartbeat stops updating,
    # the container has lost its external eyes on itself. This must wake
    # the Sentinel immediately regardless of other conditions.
    "guardian:heartbeat_stale",
    # Awareness IS the monitoring system. If it's down, Genesis is blind
    # to all other failures — this is a defense mechanism failure.
    "awareness:tick_overdue",
}

# Alert IDs for cascading/compounding failures (Tier 2).
# Only ids the health emitter actually produces belong here — the historic
# entries (memory:critical, disk:critical, embedding:unreachable,
# qdrant:unreachable, qdrant:collections_missing) matched no emitter id and
# only "worked" through the CRITICAL blanket rule below; they were removed
# when capability scoping landed. service:watchdog_failing is real and is
# listed so its WARNING-severity emission still tiers at 2.
_TIER2_PATTERNS = {
    "service:watchdog_failing",
}

# Non-remediable CRITICALs are dropped at every awareness tick — log each
# dropped id once per process so the scope decision is visible in the
# journal without spamming it.
_LOGGED_DROPPED_IDS: set[str] = set()


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


def classify_alerts(
    alerts: list[dict],
    scope: frozenset[str],
) -> list[FireAlarm]:
    """Classify health alerts into fire alarm tiers.

    Args:
        alerts: List of alert dicts from health_alerts MCP (each has
                id, severity, message).
        scope: Available remediation tool ids — callers pass
               ``remediation_map.available_tools()`` (the dispatcher does)
               or an explicit frozenset (tests). Deliberately a REQUIRED
               argument: a default that auto-detects would let a forgotten
               call site silently pick a polarity.

    Returns:
        Fire alarms sorted by tier (worst first). Empty list if no alarms.
        Alerts with no available remediation tool are never included —
        the Sentinel is only woken for what it can act on.
    """
    alarms: list[FireAlarm] = []

    for alert in alerts:
        alert_id = alert.get("id", "")
        severity = alert.get("severity", "").upper()
        message = alert.get("message", "")

        # Stage 1 — scope: no remediation tool, no fire alarm.
        if not is_remediable(alert_id, scope):
            if severity == "CRITICAL" and alert_id not in _LOGGED_DROPPED_IDS:
                _LOGGED_DROPPED_IDS.add(alert_id)
                logger.info(
                    "Sentinel scope: CRITICAL alert %r has no available "
                    "remediation tool — not a fire alarm (stays visible "
                    "on user-facing surfaces)",
                    alert_id,
                )
            continue

        # Stage 2 — tier. Tier 1: Defense mechanism failures.
        if alert_id in _TIER1_PATTERNS:
            alarms.append(FireAlarm(tier=1, alert_id=alert_id, severity=severity, message=message))
            continue

        # Tier 2: known cascading patterns OR any CRITICAL severity from a
        # trusted emitter. For in-scope alerts, severity is the contract:
        # if mcp/health/errors.py emits something at CRITICAL, it MEANS
        # critical, and Sentinel should wake for it. Dishonest emitters get
        # fixed at the source (e.g. the call_site:* source-gating and the
        # disabled-backup gate in mcp/health/errors.py), and non-remediable
        # alerts are excluded by the scope stage above — never by silencing
        # legitimate in-scope CRITICALs here. The regression test
        # test_all_emitted_critical_ids_classify pins every emitted CRITICAL
        # to its expected disposition.
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
