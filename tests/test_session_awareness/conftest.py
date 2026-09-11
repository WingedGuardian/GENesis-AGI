"""Shared fixtures/helpers for session_awareness tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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


@pytest.fixture()
def production_dirs():
    """The UNPATCHED module constants, for invariant tests. Captured from
    the modules before the autouse hermetic fixture below re-points them
    (fixture ordering: this one reads the originals at import scope)."""
    return dict(_PRODUCTION_DIRS)


@pytest.fixture(autouse=True)
def _hermetic_background_session_dir(tmp_path, monkeypatch):
    """Keep every headless spawn's cwd out of the real HOME.

    run_headless_json now runs children from background_session_dir(),
    whose accessor mkdirs under ``Path.home()`` — an autouse redirect
    keeps the whole suite hermetic instead of provisioning a real
    ``~/.genesis/background-sessions`` on every test machine."""
    import genesis.cc.types as _cc_types
    import genesis.session_awareness.headless as _headless

    monkeypatch.setattr(
        _cc_types, "_BACKGROUND_SESSION_DIR", tmp_path / "bg-sessions"
    )
    monkeypatch.setattr(_headless, "_AMBIENT_JUDGE_ROOT", tmp_path / "ambient-judges")


# Captured at import time — before any fixture patches the modules — so the
# production_dirs fixture hands tests the REAL constants, not tmp paths.
import genesis.cc.types as _cc_types_orig  # noqa: E402
import genesis.session_awareness.headless as _headless_orig  # noqa: E402

_PRODUCTION_DIRS = {
    "background": _cc_types_orig._BACKGROUND_SESSION_DIR,
    "judge": _headless_orig._AMBIENT_JUDGE_ROOT,
}
