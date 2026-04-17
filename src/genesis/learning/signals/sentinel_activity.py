"""SentinelActivityCollector — reports Sentinel state as awareness signal."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from genesis.awareness.types import SignalReading

logger = logging.getLogger(__name__)

# Sentinel state → signal value mapping.
# Higher values = more concerning states that warrant Micro attention.
_STATE_VALUES = {
    "healthy": 0.0,
    "investigating": 0.3,
    "remediating": 0.7,
    "escalated": 1.0,
}


class SentinelActivityCollector:
    """Reports current Sentinel state as a 0.0–1.0 signal.

    Reads the persistent state file written by the Sentinel dispatcher.
    Uses the existing load_state() function which handles missing/corrupt
    files gracefully (returns HEALTHY default).
    """

    signal_name = "sentinel_activity"

    def __init__(self, *, state_path: Path | None = None) -> None:
        self._state_path = state_path

    async def collect(self) -> SignalReading:
        try:
            from genesis.sentinel.state import load_state

            state_data = load_state(path=self._state_path)
            value = _STATE_VALUES.get(state_data.current_state, 0.0)
        except Exception:
            logger.error("SentinelActivityCollector failed", exc_info=True)
            value = 0.0

        return SignalReading(
            name=self.signal_name,
            value=value,
            source="sentinel_state",
            collected_at=datetime.now(UTC).isoformat(),
        )
