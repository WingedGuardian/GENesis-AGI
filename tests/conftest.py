"""Shared test fixtures for Genesis v3."""

# ── sys.path guard: tests must import from THIS worktree, not from main ──
# The venv has an editable install (``pip install -e .``) whose ``.pth``
# file adds ``/home/ubuntu/genesis/src`` — the MAIN worktree's src — to
# ``sys.path`` at interpreter startup. Without this guard, running
# ``pytest`` from a sibling worktree collects tests from the worktree's
# ``tests/`` directory but imports ``genesis.*`` from main's source tree.
# The tests silently lie: they report PASS/FAIL against the wrong code.
#
# This block inserts the current worktree's own ``src`` at ``sys.path``
# position 0 before any test collects. pytest loads conftest.py during
# the collection phase, before any test module runs, and before the
# fixtures below import ``genesis.*``. Position 0 beats the editable
# install's path.
#
# Safety:
# - In the main worktree ``_WORKTREE_SRC`` resolves to the same directory
#   as the editable install's ``.pth``-injected path. The guard removes
#   and re-inserts that path at position 0 — a reorder, not a true no-op,
#   but semantically equivalent because only one ``genesis/`` package
#   exists on ``sys.path``. Import resolution is unchanged.
# - In a sibling worktree it shadows the editable install so tests resolve
#   against the worktree's source tree, which is what every test author
#   expects.
# - This is the structural fix for the 2026-04-10 worktree-test-isolation
#   footgun: before this guard, every sibling-worktree test run needed an
#   explicit ``PYTHONPATH=src`` prefix or it silently tested main instead.
import sys
from pathlib import Path

_WORKTREE_SRC = Path(__file__).resolve().parent.parent / "src"
if _WORKTREE_SRC.is_dir():
    _src_str = str(_WORKTREE_SRC)
    if _src_str in sys.path:
        # Already present but may not be at position 0 — move it to the
        # front so it shadows anything the editable install injected.
        sys.path.remove(_src_str)
    sys.path.insert(0, _src_str)

import os  # noqa: E402

import aiosqlite  # noqa: E402
import pytest  # noqa: E402

# ── Safety: prevent os.killpg(1, ...) from killing all processes ─────────
_real_killpg = os.killpg


def _safe_killpg(pgid: int, sig: int) -> None:
    """Safety wrapper that blocks os.killpg with pgid <= 1."""
    if pgid <= 1:
        raise ValueError(
            f"BLOCKED: os.killpg({pgid}, {sig}) would kill all user processes. "
            "Always set mock_proc.pid to an explicit value > 1 in tests."
        )
    _real_killpg(pgid, sig)


os.killpg = _safe_killpg  # type: ignore[assignment]


# ── Safety: prevent tests from polluting production circuit breaker state ──
@pytest.fixture(autouse=True)
def _isolate_circuit_breaker_state(tmp_path, monkeypatch):
    """Redirect circuit breaker state file to tmp_path for all tests."""
    import genesis.routing.circuit_breaker as cb_mod

    monkeypatch.setattr(cb_mod, "_STATE_FILE", tmp_path / "cb_state.json")


@pytest.fixture
async def db():
    """In-memory SQLite database with all tables created and seeded."""
    from genesis.db.schema import create_all_tables, seed_data

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(conn)
    await seed_data(conn)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture
async def empty_db():
    """In-memory SQLite database with tables but no seed data."""
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(conn)
    await conn.commit()
    yield conn
    await conn.close()
