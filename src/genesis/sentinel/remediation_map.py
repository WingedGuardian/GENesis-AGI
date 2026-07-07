"""Remediation map — which alerts the Sentinel can actually act on.

The Sentinel is Genesis's emergency responder: it exists for short-term
emergencies that threaten the continuous operation of the container, and it
is woken ONLY for conditions it has a real remediation path for. Anything it
cannot act on — remote backup targets, provider billing/quota, third-party
API outages — is not a fire alarm: those stay fully visible to the user and
the ego through the dashboard, health digest, and outreach escalation, but
they never wake the firefighter.

Naming note: this is deliberately NOT called "capabilities" — in Genesis that
word means the autonomy grant matrix (``autonomy/capabilities.py``,
``runtime/_capabilities.py``, ``~/.genesis/capabilities.json``). This module
describes *infrastructure reach of this install*, a different axis entirely.

Three pieces:

- ``TOOLS`` — each :class:`RemediationTool` names something the Sentinel can
  do, with a detector answering "is this available on THIS install right
  now?". Detectors are cheap filesystem checks (they run once per awareness
  tick) and answer "can we act on it", never "is it healthy" — a tool must
  not vanish exactly when it is needed.
- ``REMEDIATION_MAP`` / ``REMEDIATION_PREFIX_MAP`` — alert id (or id prefix)
  → the set of tools, ANY of which makes the alert remediable.
- ``UNMAPPED_BY_DESIGN`` — alert ids/prefixes deliberately left unmapped,
  each with the reason. The coverage test in
  ``tests/test_sentinel/test_classifier.py`` extracts every alert id the
  health emitter can produce and requires it to appear in the map or here —
  so a new CRITICAL emitter cannot silently bypass the Sentinel, and a
  deliberate exclusion is always an explicit, reviewed decision.

An alert id in neither table is **fail-closed**: it never becomes a fire
alarm, regardless of severity (and the coverage test fails until it is
placed). Direct escalations (``SentinelDispatcher.escalate_direct``, used by
the Guardian watchdog and remediation-registry exhaustion) bypass this map
by design — the caller has already exercised judgment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemediationTool:
    """One thing the Sentinel can do, with an availability detector."""

    id: str
    description: str
    detector: Callable[[], bool]


def _always() -> bool:
    return True


def _qdrant_local_unit_present() -> bool:
    """Local qdrant systemd unit exists → Sentinel can restart it.

    This is "can we act on qdrant", NOT "is qdrant up" — during a qdrant
    outage the unit file is still there, which is exactly when the tool
    matters. Installs vary on unit scope (this one uses a user unit), so
    check both user and system locations.
    """
    candidates = (
        Path.home() / ".config/systemd/user/qdrant.service",
        Path("/etc/systemd/system/qdrant.service"),
        Path("/usr/lib/systemd/system/qdrant.service"),
        Path("/lib/systemd/system/qdrant.service"),
    )
    return any(p.exists() for p in candidates)


def _guardian_configured() -> bool:
    """Guardian remote config exists → Sentinel can restart it over SSH."""
    return (Path.home() / ".genesis" / "guardian_remote.yaml").exists()


def _host_resource_alloc() -> bool:
    """Host-level resource allocation (grow disk/RAM from the hypervisor).

    Available when the container's ``guardian_remote.yaml`` opts in with
    ``provisioning: true`` — the install-local flag that the approval-gated
    Proxmox provisioning path (guardian gateway provision-* verbs +
    ``provision_grow`` MCP tool) is wired on THIS install. A file-flag, not a
    live gateway probe: cheap per awareness tick and stable through hypervisor
    blips, so the tool doesn't vanish exactly when the pool is filling.
    """
    path = Path.home() / ".genesis" / "guardian_remote.yaml"
    try:
        import yaml
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001 — missing/unreadable/malformed ⇒ unavailable
        return False
    return bool(data.get("provisioning"))


TOOLS: tuple[RemediationTool, ...] = (
    RemediationTool(
        "container.services",
        "Inspect and restart systemd user services inside the container",
        _always,
    ),
    RemediationTool(
        "container.disk_reclaim",
        "Reclaim container disk (prune caches, worktrees, temp files)",
        _always,
    ),
    RemediationTool(
        "container.process_control",
        "Find and stop runaway processes inside the container",
        _always,
    ),
    RemediationTool(
        "container.db_local",
        "Inspect and repair local SQLite state (queues, stuck rows)",
        _always,
    ),
    RemediationTool(
        "qdrant.local",
        "Restart the local Qdrant systemd service",
        _qdrant_local_unit_present,
    ),
    RemediationTool(
        "guardian.ssh_restart",
        "Restart the host-side Guardian over SSH (guardian-gateway)",
        _guardian_configured,
    ),
    RemediationTool(
        "host.resource_alloc",
        "Grow this VM's disk/RAM from the hypervisor (approval-gated Proxmox "
        "provisioning; propose the grow to the user)",
        _host_resource_alloc,
    ),
)


# Alert id → tools, ANY of which makes the alert remediable.
# Exact ids first; dynamic-suffix families live in the prefix map below.
REMEDIATION_MAP: dict[str, frozenset[str]] = {
    "service:genesis_down": frozenset({"container.services"}),
    "service:watchdog_blind": frozenset({"container.services"}),
    "service:watchdog_failing": frozenset({"container.services"}),
    "service:health_data_uninitialized": frozenset({"container.services"}),
    "awareness:tick_overdue": frozenset({"container.services"}),
    "guardian:heartbeat_stale": frozenset({"guardian.ssh_restart"}),
    "infra:disk_low": frozenset({"container.disk_reclaim", "host.resource_alloc"}),
    "infra:container_memory_high": frozenset(
        {"container.process_control", "host.resource_alloc"}
    ),
    "infra:qdrant_collections_missing": frozenset({"qdrant.local"}),
    "provider:qdrant_unreachable": frozenset({"qdrant.local"}),
    "genesis:update_failed": frozenset({"container.services"}),
}

# Alert id PREFIX → tools, for families with dynamic suffixes.
REMEDIATION_PREFIX_MAP: dict[str, frozenset[str]] = {
    # Genesis's own scheduler jobs silently failing — internal, restartable.
    "job_stale:": frozenset({"container.services"}),
    # Quarantined scheduler jobs — internal state, repairable.
    "job:quarantined:": frozenset({"container.services", "container.db_local"}),
    # Queue depth / dead letters — local SQLite state.
    "queue:": frozenset({"container.db_local"}),
}

# Alert ids/prefixes (a trailing ":" marks a prefix) deliberately excluded
# from Sentinel scope, with the reason. These stay user-visible: the alerts
# still appear on the dashboard and health digest, and the ids below are on
# the outreach immediate-escalation whitelist (outreach/config.py) so real
# failures still ping the user — they just never wake the firefighter.
UNMAPPED_BY_DESIGN: dict[str, str] = {
    "backup:": (
        "Backup targets are external (remote git repo / S3 / NAS) — both "
        "the target and the credentials live outside the container. "
        "Escalate-notify only."
    ),
    "provider:credit_exhaustion:": (
        "Provider billing. Refilling credits is a user action, and a "
        "financial one (no-unsanctioned-transactions rule). Same reasoning "
        "as the long-standing cc:quota_exhausted exclusion."
    ),
    "provider:embedding_failing": (
        "The embedding provider is a remote API — a third-party outage is "
        "not remediable from inside the container."
    ),
    "cc:budget": (
        "The Sentinel's only response tool IS a CC session; waking it for "
        "CC budget/quota problems is self-defeating (see classifier note)."
    ),
    "cc:quota_exhausted": (
        "Self-defeating loop — see cc:budget / classifier Tier-1 note."
    ),
    "infra:ollama_model_mismatch": (
        "Ollama is optional and typically runs on a separate machine — "
        "model pulls there are not the container's to make."
    ),
    "backup:tier2_unconfigured": (
        "Backup configuration guidance, not an emergency (WARNING-level; "
        "also covered by the backup: prefix rule above)."
    ),
    "genesis:update_available": (
        "INFO-level notice; updating is a user/steward decision."
    ),
    "call_site:": (
        "Provider call sites going down is dominated by external API "
        "outages the Sentinel cannot fix (user decision 2026-07-04: an API "
        "site being down is for the user/ego to know about, not the "
        "firefighter). In-container circuit-breaker resets can return as a "
        "dedicated tool later if the stuck-breaker case proves real."
    ),
}


def available_tools() -> frozenset[str]:
    """Evaluate every detector and return the available tool ids.

    Called once per fire-alarm check (~5 min awareness tick) — detectors
    are cheap filesystem checks, so no caching is needed. A detector that
    raises is treated as unavailable (fail-closed) and logged.
    """
    available: set[str] = set()
    for tool in TOOLS:
        try:
            if tool.detector():
                available.add(tool.id)
        except Exception:
            logger.warning(
                "Remediation tool detector %s raised — treating as unavailable",
                tool.id, exc_info=True,
            )
    return frozenset(available)


def required_tools(alert_id: str) -> frozenset[str] | None:
    """The tool set mapped for this alert id, or None if unmapped."""
    exact = REMEDIATION_MAP.get(alert_id)
    if exact is not None:
        return exact
    for prefix, tools in REMEDIATION_PREFIX_MAP.items():
        if alert_id.startswith(prefix):
            return tools
    return None


def is_remediable(alert_id: str, scope: frozenset[str]) -> bool:
    """True iff the alert maps to at least one AVAILABLE tool.

    Unmapped → False (fail-closed): an alert nobody has declared a
    remediation path for must not wake the Sentinel, no matter how loud.
    """
    required = required_tools(alert_id)
    if required is None:
        return False
    return bool(required & scope)
