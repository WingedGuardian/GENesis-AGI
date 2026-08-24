"""Pure liveness verdicts for scheduled subsystems — DB-free, trivially testable.

Two verdict shapes share one conservative threshold model. They live together (and
the shared primitives live HERE, in the cross-cutting observability layer, so the
ego module depends DOWN onto observability, never the reverse):

- ``compute_ego_liveness`` (in :mod:`genesis.ego.liveness`) — INTENT-vs-completion
  lag, for the ego whose intent (``last_proactive_fire``) is recorded independently
  of success, so a deadlocked ego shows intent racing ahead of a frozen completion.

- ``compute_pulse_liveness`` (here) — NOW-vs-``last_success`` for a scheduled job
  whose success PULSE fires every cycle regardless of work done (e.g.
  ``surplus_dispatch`` records ``record_job_success`` unconditionally at loop entry
  every ~5 min). For such a job ``last_run`` is coupled to ``last_success``, so the
  ego intent-lag would only ever see failure-streaks — it CANNOT see a stopped loop.
  The honest wedged-detector is instead "the pulse went stale": ``last_success``
  older than a conservative threshold, with the two legitimate freezes excluded —
  ``paused`` (the loop skips its success record while globally paused) and
  ``last_success is None`` (never once succeeded — owned by the ``job_never_succeeded``
  alarm, and a fresh install must never read stalled).

**Coverage boundary (deliberate).** Blind to TOTAL cessation of a job that has no
independent pulse — but ``surplus_dispatch``'s unconditional per-cycle success IS
that pulse, so a frozen ``last_success`` genuinely means "the loop stopped firing /
a dispatch hung", with no benign interpretation. **``paused`` is read at the snapshot
instant**, so a >threshold suppression that JUST ended yields a bounded transient
false-stall until the next cycle refreshes ``last_success`` (≤ one interval; the 3h
floor makes it rare) — the same self-clearing edge the ego helper documents. The
same bounded transient applies after a server RESTART that followed >threshold of
downtime (host reboot / overnight power-off): ``last_success`` is stale from before
the shutdown and the first scheduled cycle is up to one interval away, so the tile
reads stalled for that window and then self-clears. This is fine for the pull-only
dashboard tile; anyone promoting this to a PUSH alert must first anchor it to a
boot-grace like the ego cadence's ``_compute_boot_first_fire`` or it will fire a real
false alarm on every cold restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Stalled only once the last success is older than this many multiples of the
# expected interval...
STALL_INTERVAL_MULTIPLE = 4
# ...and never before this hard floor, so a transient (a single missed cycle) never
# trips and a fast cadence (surplus's 5 min → 20 min) still needs 3h of silence.
STALL_FLOOR_MINUTES = 3 * 60


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def stall_threshold_minutes(current_interval_minutes: float) -> float:
    """The conservative overdue threshold for a given expected interval."""
    try:
        interval = float(current_interval_minutes)
    except (TypeError, ValueError):
        interval = 0.0
    return max(interval * STALL_INTERVAL_MULTIPLE, float(STALL_FLOOR_MINUTES))


@dataclass(frozen=True)
class PulseLiveness:
    """Computed liveness verdict for one pulse-recording scheduled job."""

    last_success_at: str | None
    stalled: bool
    overdue_minutes: float | None
    threshold_minutes: float
    reason: str | None


def compute_pulse_liveness(
    *,
    last_success_at: str | None,
    expected_interval_minutes: float,
    paused: bool,
    now: datetime | None = None,
) -> PulseLiveness:
    """Return the liveness verdict for one pulse-recording job.

    Stalled iff (not ``paused``) and ``last_success`` is set and older than
    ``stall_threshold_minutes(expected_interval_minutes)``. ``last_success`` None
    (never succeeded) → never *stalled* — but a job that never pulsed cannot be
    confirmed alive, so the CALLER must treat a None ``last_success`` as UNAVAILABLE
    (unknown), NEVER as healthy: a scheduler that crashed before its first success
    would otherwise read green (see genesis/observability/snapshots/surplus.py).
    Callers pass ``paused`` from live runtime state; an inability to determine it
    must likewise be handled by the caller as UNKNOWN (fail-loud), never by passing
    ``paused=False`` (which would false-RED a stale but legitimately-suppressed job).
    """
    now = now or datetime.now(UTC)
    threshold = stall_threshold_minutes(expected_interval_minutes)
    success = _parse(last_success_at)

    # Never once succeeded (fresh install / login expired on day one): not stalled
    # here — the job_never_succeeded alarm owns that case.
    if success is None:
        return PulseLiveness(last_success_at, False, None, threshold, None)

    overdue = (now - success).total_seconds() / 60.0

    # Paused legitimately freezes the success pulse — never a stall.
    if paused:
        return PulseLiveness(last_success_at, False, overdue, threshold, None)

    if overdue > threshold:
        hours = overdue / 60.0
        reason = (
            f"no completed cycle in {hours:.1f}h "
            f"(expected every ~{int(expected_interval_minutes)}m)"
        )
        return PulseLiveness(last_success_at, True, overdue, threshold, reason)

    return PulseLiveness(last_success_at, False, overdue, threshold, None)
