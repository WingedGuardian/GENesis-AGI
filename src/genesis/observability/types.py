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


class ProbeStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


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
