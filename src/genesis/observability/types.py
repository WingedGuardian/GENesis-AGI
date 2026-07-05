"""Observability type definitions — enums and frozen dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Subsystem(StrEnum):
    ROUTING = "routing"
    AWARENESS = "awareness"
    SURPLUS = "surplus"
    MEMORY = "memory"
    HEALTH = "health"
    PERCEPTION = "perception"
    LEARNING = "learning"
    INBOX = "inbox"
    REFLECTION = "reflection"
    PROVIDERS = "providers"
    WEB = "web"
    OUTREACH = "outreach"
    DASHBOARD = "dashboard"
    EGO = "ego"
    AUTONOMY = "autonomy"
    RECON = "recon"
    MAIL = "mail"
    OBSERVABILITY = "observability"
    GUARDIAN = "guardian"
    SENTINEL = "sentinel"


class ProbeStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


# Health-status ranking (higher = worse) for composing multiple signals and for
# classifying probe transitions. Shared so snapshot code and the probe-transition
# tracker rank identically. Unknown/unavailable rank 1 = "no signal", distinct
# from healthy(0) and from degraded/down(2/3).
STATUS_RANK: dict[str, int] = {
    "healthy": 0,
    "unknown": 1,
    "unavailable": 1,
    "degraded": 2,
    "down": 3,
    "error": 3,
}


def status_class(status: str) -> str | None:
    """Collapse a raw probe status to a coarse health class for transition
    detection: "healthy" (rank 0), "unhealthy" (rank >= 2), or None for
    rank-1 "no signal" states (unknown/unavailable) which must NOT count as a
    crossing — a probe flickering healthy->unknown->healthy should emit nothing.
    """
    rank = STATUS_RANK.get(status)
    if rank is None or rank == 1:
        return None
    return "healthy" if rank == 0 else "unhealthy"


@dataclass(frozen=True)
class GenesisEvent:
    """A single observability event emitted by a Genesis subsystem."""

    subsystem: Subsystem
    severity: Severity
    event_type: str  # e.g. "breaker.tripped", "budget.exceeded"
    message: str
    timestamp: str  # ISO datetime
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    """Result of a single health probe."""

    name: str
    status: ProbeStatus
    latency_ms: float
    message: str = ""
    checked_at: str = ""  # ISO datetime
    details: dict | None = None


@dataclass(frozen=True)
class SubsystemStatus:
    """Status summary for one subsystem."""

    subsystem: Subsystem
    healthy: bool
    detail: str = ""


@dataclass(frozen=True)
class SystemSnapshot:
    """Point-in-time system health snapshot."""

    timestamp: str  # ISO datetime
    probes: list[ProbeResult] = field(default_factory=list)
    subsystems: list[SubsystemStatus] = field(default_factory=list)
    overall_healthy: bool = True
