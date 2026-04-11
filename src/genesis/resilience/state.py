"""Composite resilience state machine — 4 independent axes with flapping protection."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum

from genesis.routing.types import DegradationLevel

logger = logging.getLogger(__name__)


# ── Axis enums (ordered worst → best for comparison) ─────────────────────────

class CloudStatus(IntEnum):
    """Cloud LLM provider availability. Lower value = worse."""
    OFFLINE = 0
    ESSENTIAL = 1
    REDUCED = 2
    FALLBACK = 3
    NORMAL = 4


class MemoryStatus(IntEnum):
    """Memory subsystem availability."""
    DOWN = 0
    WRITE_QUEUED = 1
    FTS_ONLY = 2
    NORMAL = 3


class EmbeddingStatus(IntEnum):
    """Embedding provider availability."""
    UNAVAILABLE = 0
    QUEUED = 1
    NORMAL = 2


class CCStatus(IntEnum):
    """Claude Code session availability."""
    UNAVAILABLE = 0
    RATE_LIMITED = 1
    THROTTLED = 2
    NORMAL = 3


# ── Transition record ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StateTransition:
    """Record of a single axis state change."""
    axis: str
    old_value: str
    new_value: str
    timestamp: str  # ISO datetime
    suppressed: bool = False  # True if flapping protection blocked this


# ── Resilience state snapshot ─────────────────────────────────────────────────

@dataclass
class ResilienceState:
    """Current composite resilience state across all axes."""
    cloud: CloudStatus = CloudStatus.NORMAL
    memory: MemoryStatus = MemoryStatus.NORMAL
    embedding: EmbeddingStatus = EmbeddingStatus.NORMAL
    cc: CCStatus = CCStatus.NORMAL
    timestamp: str = ""
    transitions: list[StateTransition] = field(default_factory=list)

    def to_legacy_degradation_level(self) -> DegradationLevel:
        """Map composite state to single DegradationLevel for backward compat."""
        # Start with cloud-based level
        cloud_map = {
            CloudStatus.NORMAL: DegradationLevel.NORMAL,
            CloudStatus.FALLBACK: DegradationLevel.FALLBACK,
            CloudStatus.REDUCED: DegradationLevel.REDUCED,
            CloudStatus.ESSENTIAL: DegradationLevel.ESSENTIAL,
            CloudStatus.OFFLINE: DegradationLevel.ESSENTIAL,
        }
        level = cloud_map[self.cloud]

        # Memory impairment overrides if worse
        if self.memory in (MemoryStatus.FTS_ONLY, MemoryStatus.WRITE_QUEUED, MemoryStatus.DOWN) and level < DegradationLevel.MEMORY_IMPAIRED:
            level = DegradationLevel.MEMORY_IMPAIRED

        # Embedding unavailability overrides if worse
        if self.embedding in (EmbeddingStatus.QUEUED, EmbeddingStatus.UNAVAILABLE) and level < DegradationLevel.LOCAL_COMPUTE_DOWN:
            level = DegradationLevel.LOCAL_COMPUTE_DOWN

        return level


# ── Flapping tracker (per-axis) ──────────────────────────────────────────────

_FLAP_WINDOW = timedelta(minutes=15)
_FLAP_THRESHOLD = 3
_STABILIZE_DURATION = timedelta(minutes=10)


@dataclass
class _AxisFlap:
    """Tracks flapping for one axis."""
    transition_times: list[datetime] = field(default_factory=list)
    stabilize_until: datetime | None = None
    held_state: int | None = None  # The worse state being held


# ── State machine ────────────────────────────────────────────────────────────

class ResilienceStateMachine:
    """Manages composite resilience state with flapping protection."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state = ResilienceState(timestamp=self._clock().isoformat())
        self._flap: dict[str, _AxisFlap] = {
            "cloud": _AxisFlap(),
            "memory": _AxisFlap(),
            "embedding": _AxisFlap(),
            "cc": _AxisFlap(),
        }

    @property
    def current(self) -> ResilienceState:
        return self._state

    def is_any_degraded(self) -> bool:
        """Return True if any axis is below NORMAL."""
        return (
            self._state.cloud != CloudStatus.NORMAL
            or self._state.memory != MemoryStatus.NORMAL
            or self._state.embedding != EmbeddingStatus.NORMAL
            or self._state.cc != CCStatus.NORMAL
        )

    def update_cloud(self, status: CloudStatus) -> list[StateTransition]:
        return self._update("cloud", status)

    def update_memory(self, status: MemoryStatus) -> list[StateTransition]:
        return self._update("memory", status)

    def update_embedding(self, status: EmbeddingStatus) -> list[StateTransition]:
        return self._update("embedding", status)

    def update_cc(self, status: CCStatus) -> list[StateTransition]:
        # CC rate limits are transient API events, not cascading provider
        # failures. Flapping protection was designed for cloud/memory/embedding
        # where genuine instability needs a stabilization hold. Applied to CC,
        # it turns normal 429-recover-429-recover API behavior into a 10-minute
        # lockout that latches RATE_LIMITED and blocks the display + emitters
        # from honestly reporting recovery. Opt out. (See Part 9a in
        # .claude/plans/fluttering-humming-bentley.md for the false-alarm
        # incident that motivated this.)
        return self._update("cc", status, apply_flapping_protection=False)

    def _update(
        self,
        axis: str,
        new_status: int,
        *,
        apply_flapping_protection: bool = True,
    ) -> list[StateTransition]:
        """Update a single axis, applying flapping protection.

        When ``apply_flapping_protection=False`` the stabilization window and
        flap-detection threshold are bypassed: transitions apply unconditionally
        and are never marked suppressed. Used for axes whose "failures" are
        transient by design (e.g., CC API rate limits).
        """
        now = self._clock()
        old_status = getattr(self._state, axis)

        if old_status == new_status:
            return []

        flap = self._flap[axis]

        if apply_flapping_protection:
            # Check if we're in stabilization period
            if flap.stabilize_until is not None and now < flap.stabilize_until:
                # During stabilization: only allow transitions to WORSE states
                if new_status > old_status:  # Higher value = better
                    transition = StateTransition(
                        axis=axis,
                        old_value=old_status.name,
                        new_value=type(old_status)(new_status).name,
                        timestamp=now.isoformat(),
                        suppressed=True,
                    )
                    self._state.transitions.append(transition)
                    logger.debug(
                        "Flapping protection: suppressed %s %s→%s (stabilizing until %s)",
                        axis, old_status.name, type(old_status)(new_status).name,
                        flap.stabilize_until.isoformat(),
                    )
                    return [transition]
                # Worse state during stabilization — accept and extend
                flap.stabilize_until = now + _STABILIZE_DURATION
            else:
                # Not in stabilization — clear expired state
                if flap.stabilize_until is not None:
                    flap.stabilize_until = None
                    flap.held_state = None
                    flap.transition_times.clear()

            # Record transition time for flap detection
            cutoff = now - _FLAP_WINDOW
            flap.transition_times = [t for t in flap.transition_times if t > cutoff]
            flap.transition_times.append(now)

            # Check if flapping threshold exceeded
            if len(flap.transition_times) > _FLAP_THRESHOLD:
                # Enter stabilization: hold current (worse) state
                worse = min(old_status, new_status)  # Lower value = worse
                flap.stabilize_until = now + _STABILIZE_DURATION
                flap.held_state = worse
                new_status = type(old_status)(worse)
                logger.warning(
                    "Flapping detected on %s (%d transitions in 15min), "
                    "holding %s for 10min stabilization",
                    axis, len(flap.transition_times), type(old_status)(worse).name,
                )

        # Apply the transition
        transition = StateTransition(
            axis=axis,
            old_value=old_status.name,
            new_value=type(old_status)(new_status).name,
            timestamp=now.isoformat(),
            suppressed=False,
        )

        setattr(self._state, axis, type(old_status)(new_status))
        self._state.timestamp = now.isoformat()
        self._state.transitions.append(transition)

        logger.info(
            "Resilience state: %s %s → %s",
            axis, old_status.name, type(old_status)(new_status).name,
        )

        return [transition]
