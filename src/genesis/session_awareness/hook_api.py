"""Hook-facing orchestration: one fail-open call per genuine user turn.

The proactive memory hook calls :func:`hook_fold` after all stdout is
flushed and every existing metric is recorded — this function must never
raise, never print, and never write anywhere except the session's own
``session_theme.json``. PR1 is record-only: fires are recorded in the
statefile; PR2 spawns the detached worker on fire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .accumulator import fold_turn
from .statefiles import load_state, save_state
from .trigger import check_fire, record_fire, stability


def hook_fold(
    *,
    session_id: str,
    vector: list[float],
    prompt_keywords: list[str],
    file_keywords: list[str] | None = None,
    pivoted: bool = False,
    base_dir: Path | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Fold one turn; check the drift trigger; record any fire.

    Returns a small summary dict (for tests and the PR4 replay harness)
    or None on any failure — by contract this function cannot raise.
    """
    try:
        if not session_id or not vector:
            return None
        now = now or datetime.now(UTC)
        now_iso = now.isoformat()

        state = load_state(session_id, base=base_dir, now=now)
        fold_turn(
            state,
            vector,
            prompt_keywords,
            list(file_keywords or []),
            pivoted=pivoted,
            now_iso=now_iso,
        )
        fired, reason = check_fire(state, now)
        if fired:
            record_fire(state, now_iso)  # PR1: record-only, no spawn
        save_state(session_id, state, base=base_dir)
        return {
            "fired": fired,
            "reason": reason,
            "ema_turns": state.get("ema_turns", 0),
            "stability": stability(state.get("ring", [])),
            "fired_count": state.get("fired_count", 0),
            "outlier_skips": state.get("outlier_skips", 0),
        }
    except Exception:
        return None
