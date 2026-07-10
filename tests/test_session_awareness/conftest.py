"""Shared fixtures/helpers for session_awareness tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from genesis.session_awareness.statefiles import empty_state, save_state

DIM = 8


def seed_theme(
    sessions_root: Path, session_id: str, *, ema: list[float] | None = None,
) -> None:
    """Write a settled theme state ready to fire.

    ``updated_at`` is NOW-relative, never hardcoded: run_worker's
    load_state compares it against the wall clock (STALE_AFTER softening
    shrinks the ring → stability 0.0). A hardcoded stamp is a time bomb —
    green when written, red for every run after the staleness horizon
    passes (broke main CI on 2026-07-10).
    """
    s = empty_state(session_id)
    s["ema"] = ema or [1.0] + [0.0] * (DIM - 1)
    s["ema_turns"] = 4
    s["ring"] = [s["ema"]] * 3
    s["entities"] = {"genesis": 2.0, "voice": 1.1, "faint": 0.06}
    s["updated_at"] = datetime.now(UTC).isoformat()
    save_state(session_id, s, base=sessions_root)
