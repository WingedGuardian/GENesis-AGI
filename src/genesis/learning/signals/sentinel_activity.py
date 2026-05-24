"""SentinelActivityCollector — reports Sentinel state as awareness signal."""

from __future__ import annotations

import json
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
    "awaiting_dispatch_approval": 0.5,
    "awaiting_action_approval": 0.5,
}

_DEFAULT_STATE_PATH = Path.home() / ".genesis" / "sentinel_state.json"


class SentinelActivityCollector:
    """Reports current Sentinel state as a 0.0–1.0 signal.

    Reads the persistent state file directly (not via load_state()) because
    load_state() applies post-restart recovery — resetting INVESTIGATING and
    REMEDIATING to HEALTHY. The signal collector needs to report the actual
    current state for awareness purposes, not the recovery-adjusted state.
    """

    signal_name = "sentinel_activity"

    def __init__(self, *, state_path: Path | None = None) -> None:
        self._state_path = state_path or _DEFAULT_STATE_PATH

    async def collect(self) -> SignalReading:
        value = 0.0
        try:
            if self._state_path.exists():
                raw = json.loads(self._state_path.read_text())
                state_str = raw.get("current_state", "healthy") if isinstance(raw, dict) else "healthy"
                value = _STATE_VALUES.get(state_str, 0.0)
        except Exception:
            logger.error("SentinelActivityCollector failed", exc_info=True)

        return SignalReading(
            name=self.signal_name,
            value=value,
            source="sentinel_state",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note="0.0=healthy (normal). 0.3=investigating, 0.7=remediating, 1.0=escalated",
        )
