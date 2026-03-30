"""Alert dispatcher — sends to all configured channels in parallel.

Logs failures but never crashes. Returns True if ANY channel delivered.
Also logs all alerts to journald as fallback (F2).
"""

from __future__ import annotations

import asyncio
import logging

from genesis.guardian.alert.base import Alert, AlertChannel, AlertSeverity

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """Dispatch alerts to all configured channels.

    Every alert is logged to journald regardless of channel success (F2).
    If all channels fail, the journal is the backup record.
    """

    def __init__(self, channels: list[AlertChannel] | None = None) -> None:
        self._channels: list[AlertChannel] = channels or []

    def add_channel(self, channel: AlertChannel) -> None:
        self._channels.append(channel)

    async def send(self, alert: Alert) -> bool:
        """Send alert to all channels. Returns True if any succeeded.

        Always logs to journal as fallback (F2 finding).
        """
        # F2: Always log to journal, regardless of channel delivery
        logger.log(
            _severity_to_log_level(alert.severity),
            "Guardian alert [%s]: %s — %s",
            alert.severity.value,
            alert.title,
            alert.body,
        )

        if not self._channels:
            logger.warning("No alert channels configured — alert only in journal")
            return False

        results = await asyncio.gather(
            *(self._safe_send(ch, alert) for ch in self._channels),
            return_exceptions=True,
        )

        successes = sum(
            1 for r in results
            if isinstance(r, bool) and r
        )

        if successes == 0:
            logger.error(
                "All %d alert channels failed for: %s",
                len(self._channels), alert.title,
            )
            return False

        return True

    async def test_all(self) -> dict[str, bool]:
        """Test connectivity for all channels. Returns {channel_class: success}."""
        results = {}
        for ch in self._channels:
            name = type(ch).__name__
            try:
                results[name] = await ch.test_connectivity()
            except Exception as exc:
                logger.warning("Channel %s connectivity test failed: %s", name, exc)
                results[name] = False
        return results

    @staticmethod
    async def _safe_send(channel: AlertChannel, alert: Alert) -> bool:
        """Send to a single channel, catching all exceptions."""
        try:
            return await channel.send(alert)
        except Exception as exc:
            logger.error(
                "Alert channel %s failed: %s",
                type(channel).__name__, exc, exc_info=True,
            )
            return False


def _severity_to_log_level(severity: AlertSeverity | str) -> int:
    """Map alert severity to Python logging level."""
    return {
        "info": logging.INFO,
        "warning": logging.WARNING,
        "critical": logging.ERROR,
        "emergency": logging.CRITICAL,
    }.get(severity, logging.WARNING)
