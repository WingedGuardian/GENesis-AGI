"""Drift trigger: pure decision function over the theme state.

``check_fire`` answers one question — has the session theme *settled*
somewhere new? — and returns a reason string for every no, so shadow
logs and the replay harness can see exactly which condition gated.

Thresholds are module constants ON PURPOSE: the PR4 replay harness is
the tuning loop, and constants keep every iteration a one-line diff.
"""

from __future__ import annotations

from datetime import datetime

from .accumulator import RING_SIZE, cosine

EMA_MIN_TURNS = 3  # don't judge a theme before it exists
STABILITY_MIN = 0.90  # min pairwise cosine across the ring
FIRED_DIST_MIN = 0.15  # cosine distance from every prior fired region
# (0.35 starved on replay: ALL topics in a real dev session sit within
# ~0.3 of each other — the arbiter, not the region gate, filters noise)
TURNS_BETWEEN_FIRES = 3
MAX_FIRES = 8  # per session (multi-day resumed sessions starve at 3)
CLAIM_STALE_S = 120.0  # pending-worker claim override


def stability(ring: list[list[float]]) -> float:
    """Min pairwise cosine across the ring; 0.0 until the ring is full."""
    if len(ring) < RING_SIZE:
        return 0.0
    return min(
        cosine(ring[i], ring[j])
        for i in range(len(ring))
        for j in range(i + 1, len(ring))
    )


def check_fire(state: dict, now: datetime) -> tuple[bool, str]:
    """(should_fire, reason). Reason is "fire" or the gating condition."""
    ema = state.get("ema")
    if ema is None:
        return False, "no_ema"
    if state.get("ema_turns", 0) < EMA_MIN_TURNS:
        return False, "warming"
    if stability(state.get("ring", [])) < STABILITY_MIN:
        return False, "unstable"
    if state.get("fired_count", 0) >= MAX_FIRES:
        return False, "max_fires"

    fired = state.get("fired", [])
    if fired:
        last_turn = fired[-1].get("turn", 0)
        if state.get("ema_turns", 0) - last_turn < TURNS_BETWEEN_FIRES:
            return False, "cooldown"
        for region in fired:
            if 1.0 - cosine(ema, region.get("ema", [])) < FIRED_DIST_MIN:
                return False, "near_fired_region"

    pending = state.get("worker_pending_since")
    if pending:
        try:
            age = (now - datetime.fromisoformat(pending)).total_seconds()
        except Exception:
            age = CLAIM_STALE_S + 1.0  # unparseable claim: treat as stale
        if age <= CLAIM_STALE_S:
            return False, "worker_pending"

    return True, "fire"


def record_fire(state: dict, now_iso: str) -> None:
    """Claim the current theme region (mutates *state*).

    PR1 is record-only; PR2's spawn happens under this same claim —
    ``worker_pending_since`` doubles as the pending-worker lock that
    ``check_fire`` honors (with the stale override).
    """
    state.setdefault("fired", []).append(
        {"ema": list(state.get("ema") or []), "turn": state.get("ema_turns", 0), "at": now_iso},
    )
    state["fired_count"] = state.get("fired_count", 0) + 1
    state["worker_pending_since"] = now_iso
