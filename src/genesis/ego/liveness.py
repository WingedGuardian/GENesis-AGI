"""Truthful ego-cycle liveness — is a cycle OVERDUE vs the current cadence?

The health surfaces historically read liveness PROXIES (the consumer-loop
``is_running`` flag, a decoupled 5-min heartbeat, ``next_fire_at``) that stay
green while the ego is deadlocked. This computes liveness from the one HONEST
signal instead: ``job_health.last_success``, which advances ONLY on a real
completed cycle (a gated / paused / failed no-op never touches it).

Conservative by construction: a cycle counts as stalled only past SEVERAL
multiples of the CURRENT (possibly backed-off) interval AND past a hard floor
that clears any quiet-hours window — so a legitimate adaptive backoff or an
overnight lull never reads as a stall. ``gated`` (waiting on the user) and
``is_paused`` (a chosen state) are legitimate non-running states, surfaced
separately, never as a stall. A fresh install with no completed cycle yet is
never stalled.

Pure and DB-free so it is trivially unit-testable; callers read
``job_health.last_success`` and pass it in. Shared by the dashboard status
endpoint and the awareness liveness alert so the two can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# A cycle is overdue only past this many multiples of the CURRENT interval...
STALL_INTERVAL_MULTIPLE = 4
# ...and never before this hard floor, which exceeds any quiet-hours window so a
# short base cadence cannot trip on one skipped tick or an overnight lull.
STALL_FLOOR_MINUTES = 12 * 60


@dataclass(frozen=True)
class EgoLiveness:
    """Computed liveness verdict for one ego cycle."""

    last_success_at: str | None
    stalled: bool
    overdue_minutes: float | None
    threshold_minutes: float
    reason: str | None


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def stall_threshold_minutes(current_interval_minutes: float) -> float:
    """The conservative overdue threshold for a given current interval."""
    try:
        interval = float(current_interval_minutes)
    except (TypeError, ValueError):
        interval = 0.0
    return max(interval * STALL_INTERVAL_MULTIPLE, float(STALL_FLOOR_MINUTES))


def compute_ego_liveness(
    *,
    last_success_at: str | None,
    current_interval_minutes: float,
    gated: bool,
    is_paused: bool,
    now: datetime | None = None,
) -> EgoLiveness:
    """Return the liveness verdict for one ego from its last real cycle.

    ``last_success_at`` is ``job_health.last_success`` (ISO). ``None`` means no
    completed cycle yet (fresh install) → never stalled. ``gated`` / ``is_paused``
    → never stalled (legitimate non-running states). Otherwise stalled iff the
    gap since the last completed cycle exceeds the conservative threshold.
    """
    now = now or datetime.now(UTC)
    threshold = stall_threshold_minutes(current_interval_minutes)
    last = _parse(last_success_at)
    if last is None:
        return EgoLiveness(last_success_at, False, None, threshold, None)
    overdue = (now - last).total_seconds() / 60.0
    if gated or is_paused:
        return EgoLiveness(last_success_at, False, overdue, threshold, None)
    if overdue > threshold:
        hours = overdue / 60.0
        reason = (
            f"no completed cycle in {hours:.1f}h (expected every ~{int(current_interval_minutes)}m)"
        )
        return EgoLiveness(last_success_at, True, overdue, threshold, reason)
    return EgoLiveness(last_success_at, False, overdue, threshold, None)
