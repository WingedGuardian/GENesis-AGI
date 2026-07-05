"""Probe-transition tracking — signal when an infra probe crosses the
healthy<->unhealthy boundary, with flapping protection and a startup grace.

Deliberately SEPARATE from ``resilience.state.ResilienceStateMachine``: that
class manages the 5 typed degradation axes that DRIVE ROUTING. Probe transitions
are raw observability and must never influence routing decisions, so they live
here, isolated by construction. Probes are binary (healthy/unhealthy via
``status_class``), so the 4-level axis flap machinery is reimplemented lean
rather than reused.

The tracker is pure/stateful and does NOT emit events itself — the caller
(``HealthDataService.build``) decides how to surface a returned transition. This
keeps the tracker trivially testable and free of an event-bus dependency.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from genesis.observability.types import status_class

# If a probe crosses the healthy<->unhealthy boundary more than _FLAP_THRESHOLD
# times within _FLAP_WINDOW, subsequent crossings are flagged flapping (still
# returned, but the caller can dampen them). Mirrors the resilience flap window.
_FLAP_WINDOW = timedelta(minutes=15)
_FLAP_THRESHOLD = 3

# Grace period after construction (server start) during which transitions update
# internal state but are NOT returned — avoids the restart transient where DB /
# Qdrant probes read down for a few seconds before services settle.
_DEFAULT_WARMUP = timedelta(seconds=90)


@dataclass(frozen=True)
class ProbeTransition:
    """A single healthy<->unhealthy crossing for one probe."""

    probe_id: str
    old_class: str        # "healthy" | "unhealthy"
    new_class: str        # "healthy" | "unhealthy"
    old_status: str       # raw status, e.g. "healthy"
    new_status: str       # raw status, e.g. "down"
    timestamp: str        # ISO datetime
    flapping: bool = False


@dataclass
class _ProbeState:
    last_class: str
    last_status: str
    crossings: deque = field(default_factory=lambda: deque(maxlen=8))


class ProbeTransitionTracker:
    """Tracks per-probe health class and reports boundary crossings.

    Not thread/async-safe by itself, but `observe` is synchronous (no awaits),
    and its sole live caller runs it inside a single-flight `snapshot()` build,
    so no lock is needed there.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        warmup: timedelta = _DEFAULT_WARMUP,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._warmup = warmup
        self._started_at = self._clock()
        self._probes: dict[str, _ProbeState] = {}

    def observe(self, probe_id: str, status: str) -> ProbeTransition | None:
        """Feed a probe's current raw status; return a transition iff its
        healthy<->unhealthy CLASS changed, else None.

        - First real observation of a probe seeds state and returns None (no
          spurious transition at boot).
        - Rank-1 "no signal" states (unknown/unavailable → class None) are
          ignored entirely: they don't seed, don't cross, don't reset — the
          probe keeps its last known class, so healthy→unknown→healthy is silent.
        - During the warmup grace, state is updated but nothing is returned.
        """
        new_class = status_class(status)
        if new_class is None:
            return None  # no-signal state — ignore

        prev = self._probes.get(probe_id)
        if prev is None:
            self._probes[probe_id] = _ProbeState(last_class=new_class, last_status=status)
            return None

        if new_class == prev.last_class:
            prev.last_status = status  # keep raw status fresh for the next record
            return None

        # Class crossing.
        now = self._clock()
        old_class = prev.last_class
        old_status = prev.last_status

        # Flap accounting within the sliding window.
        cutoff = now - _FLAP_WINDOW
        while prev.crossings and prev.crossings[0] < cutoff:
            prev.crossings.popleft()
        prev.crossings.append(now)
        flapping = len(prev.crossings) > _FLAP_THRESHOLD

        prev.last_class = new_class
        prev.last_status = status

        # Warmup: state is now updated, but suppress the emit itself.
        if now < self._started_at + self._warmup:
            return None

        return ProbeTransition(
            probe_id=probe_id,
            old_class=old_class,
            new_class=new_class,
            old_status=old_status,
            new_status=status,
            timestamp=now.isoformat(),
            flapping=flapping,
        )
