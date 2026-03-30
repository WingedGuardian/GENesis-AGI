"""Alert types and channel interface."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class AlertSeverity(StrEnum):
    """Alert severity levels."""

    INFO = "info"           # Status update, resolved
    WARNING = "warning"     # Degradation detected
    CRITICAL = "critical"   # System down, recovering
    EMERGENCY = "emergency" # Recovery failed, need human


@dataclass(frozen=True)
class Alert:
    """An alert to be sent through one or more channels."""

    severity: AlertSeverity
    title: str
    body: str
    approval_url: str | None = None
    failed_probes: list[str] = field(default_factory=list)
    duration_s: float | None = None
    likely_cause: str | None = None
    proposed_action: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class AlertChannel(abc.ABC):
    """Abstract base class for alert delivery channels.

    Implementations: TelegramAlertChannel, (future) Discord, webhook, email.
    Each channel handles its own formatting and delivery.
    """

    @abc.abstractmethod
    async def send(self, alert: Alert) -> bool:
        """Send an alert. Returns True if delivered successfully."""

    @abc.abstractmethod
    async def test_connectivity(self) -> bool:
        """Test that this channel can deliver messages."""
