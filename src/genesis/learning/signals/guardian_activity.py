"""GuardianActivityCollector — reports Guardian heartbeat freshness."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from genesis.awareness.types import SignalReading

logger = logging.getLogger(__name__)

_DEFAULT_HEARTBEAT_PATH = Path.home() / ".genesis" / "guardian_heartbeat.json"

# Staleness thresholds in seconds.
_STALE_THRESHOLD_S = 300     # 5 minutes — Guardian may be delayed
_VERY_STALE_THRESHOLD_S = 1800  # 30 minutes — Guardian likely dead


class GuardianActivityCollector:
    """Reports Guardian heartbeat freshness as a 0.0–1.0 signal.

    Reads ~/.genesis/guardian_heartbeat.json (written by Guardian on host VM
    via SSH into container).

    | Heartbeat Age | Signal Value |
    |--------------|-------------|
    | < 5 min      | 0.0         |
    | 5-30 min     | 0.5         |
    | > 30 min     | 1.0         |
    | File missing | 0.0         |
    | Corrupt JSON | 0.5         |

    FileNotFoundError → 0.0 is intentional: no heartbeat file means Guardian
    was never configured, which is a valid state (not an alarm).
    """

    signal_name = "guardian_activity"

    def __init__(self, *, heartbeat_path: Path | None = None) -> None:
        self._path = heartbeat_path or _DEFAULT_HEARTBEAT_PATH

    async def collect(self) -> SignalReading:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            # Guardian never ran — not an alarm condition
            return self._reading(0.0, "no_heartbeat_file")
        except OSError:
            logger.warning("GuardianActivityCollector: cannot read heartbeat file", exc_info=True)
            return self._reading(0.5, "read_error")

        try:
            data = json.loads(raw)
            timestamp_str = data.get("timestamp", "")
            hb_time = datetime.fromisoformat(timestamp_str)
            age_s = (datetime.now(UTC) - hb_time).total_seconds()
        except (json.JSONDecodeError, ValueError, TypeError):
            # Corrupt JSON — treat as stale
            return self._reading(0.5, "corrupt_heartbeat")

        if age_s < _STALE_THRESHOLD_S:
            return self._reading(0.0, f"fresh_{age_s:.0f}s")
        elif age_s < _VERY_STALE_THRESHOLD_S:
            return self._reading(0.5, f"stale_{age_s:.0f}s")
        else:
            return self._reading(1.0, f"very_stale_{age_s:.0f}s")

    def _reading(self, value: float, source: str) -> SignalReading:
        return SignalReading(
            name=self.signal_name,
            value=value,
            source=source,
            collected_at=datetime.now(UTC).isoformat(),
        )
