"""Shared fixtures/helpers for session_awareness tests."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from genesis.session_awareness.statefiles import empty_state, save_state

DIM = 8

#: Every migration that shapes ``pr_verifications``, in id order.
#:
#: It lives in the shared conftest rather than in one test module because the
#: failure it prevents is CROSS-FILE: an adversarial review MEASURED the CRUD
#: suite running this table at fourteen columns while the lane suite ran it at
#: ten, each applying its own hand-maintained list. Both were green — the lane's
#: ``SELECT *`` reads by name — so nothing would have caught the divergence, and
#: a "ONE list" comment inside a single test module is true of that module and
#: false of the table. Any suite that needs this table calls
#: :func:`build_pr_verifications`; adding a migration then reaches every caller.
PR_VERIFICATION_MIGRATIONS = tuple(
    importlib.import_module(f"genesis.db.migrations.{name}")
    for name in (
        "20260906234824_pr_verifications",
        "20260926061637_pr_verification_verdict",
    )
)


async def build_pr_verifications(conn) -> None:
    """Apply the migrated build path for ``pr_verifications``, in id order."""
    for mig in PR_VERIFICATION_MIGRATIONS:
        await mig.up(conn)


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
    # The judge cwd resolves through genesis_home() at CALL time now, so the
    # redirect is the environment variable rather than a module constant —
    # which also means the test exercises the real resolution path instead of
    # stepping over it.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "genesis-home"))
    _ = _headless  # imported for the assertion below that the module loads


# Captured at import time — before any fixture patches the modules — so the
# production_dirs fixture hands tests the REAL constants, not tmp paths.
import genesis.cc.types as _cc_types_orig  # noqa: E402
import genesis.session_awareness.headless as _headless_orig  # noqa: E402

_PRODUCTION_DIRS = {
    "background": _cc_types_orig._BACKGROUND_SESSION_DIR,
    # Resolved here, at import scope, before the autouse fixture sets
    # GENESIS_HOME — so the invariant test sees the REAL production path.
    "judge": _headless_orig._ambient_judge_dir(),
}
