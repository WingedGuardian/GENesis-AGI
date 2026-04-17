"""Sentinel state machine — 4-state lifecycle for the container-side guardian.

States:
  HEALTHY       — No fire alarms, everything normal
  INVESTIGATING — Fire alarm detected, evaluating conditions and trying reflexes
  REMEDIATING   — Reflexes failed, CC diagnosis session dispatched
  ESCALATED     — CC failed, observation created for ego. Auto-resets after timeout.

Persistence: atomic JSON write to ~/.genesis/sentinel_state.json
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILE = Path.home() / ".genesis" / "sentinel_state.json"


class SentinelState(Enum):
    HEALTHY = "healthy"
    INVESTIGATING = "investigating"
    REMEDIATING = "remediating"
    ESCALATED = "escalated"


@dataclass
class SentinelStateData:
    """Persistent state for the Sentinel dispatcher.

    Cooldowns and daily budgets have been removed. Cadence is now governed
    by per-pattern exponential backoff held in memory by the dispatcher
    (see SentinelDispatcher._pattern_attempts). Backoff state resets on
    process restart — this is intentional: a restart is a natural
    opportunity to give a stuck pattern another try.
    """

    current_state: str = "healthy"
    entered_at: str = ""
    last_trigger_source: str = ""
    last_trigger_reason: str = ""

    # Escalation tracking (auto-reset from ESCALATED back to HEALTHY)
    escalated_count: int = 0
    max_escalated_resets: int = 3
    escalated_timeout_s: int = 600  # 10 min auto-reset

    # Last dispatch timestamp (observability only — not a gate)
    last_cc_dispatch_at: str = ""

    # Rejection tracking — pattern → ISO timestamp when suppression expires.
    # Prevents rejected dispatch patterns from re-triggering within the window.
    rejected_patterns: dict[str, str] = field(default_factory=dict)

    # Bootstrap grace
    bootstrap_grace_s: int = 240
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def state(self) -> SentinelState:
        try:
            return SentinelState(self.current_state)
        except ValueError:
            return SentinelState.HEALTHY

    def transition(self, new_state: SentinelState, *, reason: str = "") -> None:
        old = self.current_state
        self.current_state = new_state.value
        self.entered_at = datetime.now(UTC).isoformat()
        if reason:
            self.last_trigger_reason = reason
        logger.info("Sentinel state: %s → %s (%s)", old, new_state.value, reason or "no reason")

    def in_bootstrap_grace(self) -> bool:
        if not self.started_at:
            return False
        try:
            started = datetime.fromisoformat(self.started_at)
            elapsed = (datetime.now(UTC) - started).total_seconds()
            return elapsed < self.bootstrap_grace_s
        except (ValueError, TypeError):
            return False

    def record_cc_dispatch(self) -> None:
        """Stamp the last dispatch time. Used for observability only."""
        self.last_cc_dispatch_at = datetime.now(UTC).isoformat()

    def should_auto_reset_escalated(self) -> bool:
        """Check if ESCALATED state should auto-reset to HEALTHY."""
        if self.state != SentinelState.ESCALATED:
            return False
        if self.escalated_count >= self.max_escalated_resets:
            return False  # Oscillation guard: stop resetting
        if not self.entered_at:
            return True
        try:
            entered = datetime.fromisoformat(self.entered_at)
            elapsed = (datetime.now(UTC) - entered).total_seconds()
            return elapsed >= self.escalated_timeout_s
        except (ValueError, TypeError):
            return True


def load_state(path: Path | None = None) -> SentinelStateData:
    """Load Sentinel state from disk. Returns fresh state on any error."""
    state_path = path or _STATE_FILE
    try:
        if state_path.exists():
            raw = json.loads(state_path.read_text())
            if isinstance(raw, dict):
                return SentinelStateData(**{
                    k: v for k, v in raw.items()
                    if k in SentinelStateData.__dataclass_fields__
                })
    except Exception:
        logger.warning("Failed to load sentinel state — resetting to HEALTHY", exc_info=True)
    return SentinelStateData()


def save_state(data: SentinelStateData, path: Path | None = None) -> None:
    """Atomic write of Sentinel state to disk."""
    state_path = path or _STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(state_path.parent), suffix=".tmp",
        )
        try:
            os.write(fd, json.dumps(asdict(data), indent=2).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_path, str(state_path))
    except OSError:
        logger.error("Failed to save sentinel state", exc_info=True)
