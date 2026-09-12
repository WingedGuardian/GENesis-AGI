"""Off-loop-readable snapshot of event-loop scheduling health.

The loop-lag sampler (``hosting/standalone.py::_loop_lag_sampler``) runs ON the
event loop and, until now, only LOGGED its drift measurement. That left no way
for an off-loop reader — a synchronous Flask worker thread, or the external
watchdog process — to answer the one question that matters during an incident:
*is the loop starved, or is the process dead?* Every ``@_async_route`` health
endpoint bounces the request back onto the same starved loop and hangs, which is
indistinguishable from a crash.

This module is the publish/read seam. The sampler calls :func:`publish` each
iteration with the latest sample; a synchronous consumer calls :func:`read` +
:func:`age_s` to classify:

  - :func:`read` is ``None``                 → UNKNOWN (sampler never ran / disabled)
  - fresh sample (small ``age_s``) + high lag → loop is STARVED but still scheduling
  - stale sample (large ``age_s``)           → loop is WEDGED (the sampler itself stalled)

Concurrency: a single module-level reference to an immutable frozen dataclass,
swapped atomically under the GIL. The writer is the loop thread; readers are
Flask worker threads (and, across a process boundary, the watchdog via the HTTP
probe). No lock is needed — the reference swap is atomic and the payload never
mutates in place. This is diagnostic-only and must never affect control flow.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class LoopHealthSample:
    """One immutable reading from the loop-lag sampler.

    ``sampled_monotonic`` is captured with :func:`time.monotonic` so
    :func:`age_s` is immune to wall-clock jumps — a growing age while the loop is
    starved is the WEDGED signal (the sampler stopped publishing because
    ``asyncio.sleep`` never returned).
    """

    drift_ms: float
    peak_ms: float
    lagging: bool
    threshold_ms: float
    executor: dict | None
    sampled_monotonic: float


_latest: LoopHealthSample | None = None
# Monotonic timestamp of the most recent LAGGING sample the sampler published.
# The sampler republishes a cleared (lagging=False) sample ~one interval after a
# stall ends, so the single ``_latest`` reference reads healthy microseconds after
# a real stall. This retains a short memory of the last lagging observation so a
# just-expired stall still counts as live evidence (see :func:`recently_lagging`).
_last_lagging_monotonic: float | None = None


def publish(sample: LoopHealthSample) -> None:
    """Store the latest sample (called from the loop thread, every interval)."""
    global _latest, _last_lagging_monotonic
    _latest = sample
    if sample.lagging:
        _last_lagging_monotonic = sample.sampled_monotonic


def recently_lagging(window_s: float, *, now: float | None = None) -> bool:
    """True if the sampler published a LAGGING sample within the last ``window_s``.

    Closes a TOCTOU race in starvation classification: because the sampler
    republishes a cleared (``lagging=False``) sample ~one interval (0.5s) after a
    stall ends, a consumer that reads only the single latest sample just after a
    stall sees the loop "healthy" and misses the real, just-observed stall. This
    reports whether lag was observed recently enough to still be live evidence.

    Only the sampler ever records a lagging observation, so this never fabricates
    starvation. It is bounded by ``window_s`` so an old, unrelated lag cannot
    suppress a genuine later timeout. ``now`` is injectable for deterministic
    tests; defaults to :func:`time.monotonic`. Diagnostic-only, like the rest of
    this module — it informs a fail-closed suppression decision, never forces one.
    """
    if _last_lagging_monotonic is None:
        return False
    ref = time.monotonic() if now is None else now
    return (ref - _last_lagging_monotonic) <= window_s


def read() -> LoopHealthSample | None:
    """Return the latest published sample, or ``None`` if none was ever published."""
    return _latest


def age_s(sample: LoopHealthSample, *, now: float | None = None) -> float:
    """Seconds since ``sample`` was taken, computed at READ time.

    ``now`` is injectable for deterministic tests; defaults to
    :func:`time.monotonic`. Clamped at 0 so a monotonic reference that predates
    the sample (should not happen, but cheap to guard) never yields a negative age.
    """
    ref = time.monotonic() if now is None else now
    return max(0.0, ref - sample.sampled_monotonic)
