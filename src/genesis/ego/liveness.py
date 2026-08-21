"""Truthful ego-cycle liveness — is the ego trying to cycle but NOT completing?

The health surfaces historically read liveness PROXIES (the consumer-loop
``is_running`` flag, a decoupled 5-min heartbeat, ``next_fire_at``) that stay
green while the ego is deadlocked. This computes liveness from two HONEST
timestamps instead:

- ``last_intent`` — ``_last_proactive_fire_at`` (persisted ego_state
  ``last_proactive_fire:<tag>``): set ONLY after a proactive tick clears every
  cadence gate (setup, functional floor, paused, global pause, circuit breaker,
  user-active/idle, quiet hours) and actually pushes a cycle signal.
- ``last_success`` — ``job_health.last_success``: advances only on a cycle that
  completes with USABLE output (a gated / paused / failed / empty-output no-op
  never touches it — ``_record_success`` vs ``_record_failure`` in cadence.py).

**Stalled = the ego's last intent-to-cycle leads its last completed cycle by
more than a conservative threshold** — i.e. it is actively trying but not
completing (the deadlock signature). Every legitimate reason the ego is not
cycling (idle because the user is active, quiet hours, paused, global pause,
circuit open, bootstrap/floor not met) prevents an *intent* from being recorded
in the first place, so it is excluded BY CONSTRUCTION — no per-condition
enumeration, and no false "stalled" for a legitimately-quiet ego. ``gated``
(a pending approval: intent recorded, but legitimately awaiting the user) is the
one non-completing state that IS recorded, so it is excluded explicitly. A fresh
install (no intent or no completed cycle yet) is never stalled.

**Coverage boundary (deliberate):** this signal detects "trying but not
completing" — the gate/consumer deadlock. It does NOT detect TOTAL cessation
(the whole cadence scheduler dying): then both timestamps freeze together, the
lag never opens, and the verdict stays healthy. That distinct failure mode
belongs to a fixed-schedule ego-heartbeat staleness monitor (the ``ego_heartbeat``
pulse keeps beating through a consumer deadlock but stops on scheduler death), so
the two signals are complementary halves. See follow-up for that monitor.

Pure and DB-free so it is trivially unit-testable; callers read the two
timestamps and pass them in. Shared by the dashboard status endpoint and the
awareness liveness alert so the two can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Stalled only once the intent→completion lag exceeds this many multiples of the
# CURRENT (possibly backed-off) interval...
STALL_INTERVAL_MULTIPLE = 4
# ...and never before this hard floor, so a transient (a single cycle awaiting a
# soon-resolving state) never trips. Suppression is handled structurally by the
# intent signal, so this floor does NOT need to cover quiet-hours windows.
STALL_FLOOR_MINUTES = 3 * 60


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
    last_intent_at: str | None,
    current_interval_minutes: float,
    gated: bool,
    now: datetime | None = None,
) -> EgoLiveness:
    """Return the liveness verdict for one ego from its intent/completion pair.

    ``last_intent_at`` is ``_last_proactive_fire_at`` (the last time the ego
    cleared every cadence gate and pushed a cycle). ``last_success_at`` is
    ``job_health.last_success``. Stalled iff (not ``gated``) and the intent leads
    completion by more than the conservative threshold. ``None`` for either (no
    proactive attempt yet, or no completed cycle yet) → never stalled.
    """
    now = now or datetime.now(UTC)
    threshold = stall_threshold_minutes(current_interval_minutes)
    intent = _parse(last_intent_at)
    success = _parse(last_success_at)

    # No proactive attempt on record (fresh, or legitimately suppressed the whole
    # time), or no completed cycle ever (fresh install): never stalled.
    if intent is None or success is None:
        return EgoLiveness(last_success_at, False, None, threshold, None)

    # How far the last COMPLETED cycle trails the last INTENT to cycle. Negative
    # (completed since the last recorded intent) or small → healthy.
    lag = (intent - success).total_seconds() / 60.0

    if gated:
        return EgoLiveness(last_success_at, False, lag, threshold, None)
    if lag > threshold:
        hours = lag / 60.0
        reason = (
            f"actively cycling but no completed cycle in {hours:.1f}h "
            f"(expected every ~{int(current_interval_minutes)}m)"
        )
        return EgoLiveness(last_success_at, True, lag, threshold, reason)
    return EgoLiveness(last_success_at, False, lag, threshold, None)
