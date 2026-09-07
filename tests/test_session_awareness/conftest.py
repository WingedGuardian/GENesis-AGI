"""Shared fixtures/helpers for session_awareness tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from genesis.session_awareness.statefiles import empty_state, save_state


@pytest.fixture(autouse=True)
def _reset_pr_verifications_table_cache():
    """pr_verifications caches its table-existence check per DB path (TRUE only).

    Every test in this package gets a fresh tmp DB, so a TRUE cached by one
    test lies to the next: the verification lane would then attempt INSERTs on
    a DB whose table was never created, the lane's own try/except would eat the
    error, and a "verification_lane_failed" note would leak into unrelated
    tests' run details — a state-leak masking failure (vacuous-test cause #6),
    invisible until a detail assertion happens to collide. Reset on BOTH sides:
    before, so this test starts honest; after, so test ORDER cannot matter.
    """
    from genesis.db.crud import pr_verifications as verif_crud

    verif_crud._tables_verified.clear()
    yield
    verif_crud._tables_verified.clear()

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
