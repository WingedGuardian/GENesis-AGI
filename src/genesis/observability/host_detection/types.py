"""Types for host framework detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class HostFrameworkStatus:
    """Result of probing for a host framework."""

    name: str
    detected: bool
    status: str = "unknown"  # healthy / degraded / down / unknown
    version: str | None = None
    uptime_seconds: float | None = None
    pid: int | None = None
    restart_cmd: str | None = None
    details: dict = field(default_factory=dict)


@runtime_checkable
class HostDetector(Protocol):
    """Protocol for host framework detectors.

    Each detector probes for a specific host framework (Agent Zero, OpenClaw,
    etc.) and returns a HostFrameworkStatus. Detectors are checked in priority
    order (lower = first). The first to return detected=True wins.
    """

    @property
    def name(self) -> str:
        """Human-readable framework name (e.g. 'Agent Zero')."""
        ...

    @property
    def priority(self) -> int:
        """Check order. Lower = checked first."""
        ...

    def detect(self) -> HostFrameworkStatus:
        """Probe for this framework. Returns status with detected=True/False."""
        ...
