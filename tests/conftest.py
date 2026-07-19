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


@pytest.fixture(autouse=True)
def _isolate_ledger_write_failures():
    """Reset the ledger writer + grader failure counters around every test.

    ``genesis.ledger.writers._write_failures`` (P1b) and the P2 grader's
    ``_metric_vanished`` / ``_grade_failed`` are process-global Counters —
    correct for production (they accumulate since process start, read by
    ``_compute_alerts``), but they leak across tests: a hook/grader-failure
    test would otherwise make an unrelated health-alert test see a stray
    ``ledger:write_failed`` / ``ledger:grade_failed`` alert. Clear before and
    after each test.
    """
    from genesis.ledger import cells as _ledger_cells
    from genesis.ledger import grader as _ledger_grader
    from genesis.ledger import writers as _ledger_writers

    _ledger_writers._write_failures.clear()
    _ledger_grader._reset_grade_failure_counts_for_tests()
    _ledger_cells._reset_cell_counters_for_tests()
    yield
    _ledger_writers._write_failures.clear()
    _ledger_grader._reset_grade_failure_counts_for_tests()
    _ledger_cells._reset_cell_counters_for_tests()


@pytest.fixture
async def db():
    """In-memory SQLite database with all tables created and seeded."""
    from genesis.db.connection import SerializedConnection
    from genesis.db.schema import create_all_tables, seed_data

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(conn)
    await seed_data(conn)
    await conn.commit()
    wrapped = SerializedConnection(conn)
    yield wrapped
    await wrapped.close()


@pytest.fixture(autouse=True)
def _guard_db_crud_not_mocked():
    """Pin the test that leaks a Mock onto a real ``genesis.db.crud`` function.

    A bare ``obs_crud.create = AsyncMock()`` — assigning a mock to a real module
    attribute *without* ``monkeypatch``/``patch`` — is never restored. It then
    silently poisons the shared ``db`` fixture for the rest of the session:
    inserts return a truthy Mock but write nothing, so a distant victim test
    reads 0 rows and fails mysteriously (cost us a multi-session hunt). This
    guard makes the leak fail at the *offending* test instead.

    Scoped to ``observations`` (the proven hotspot + highest-traffic crud
    module). Autouse fixtures tear down *after* explicitly-requested fixtures,
    so a legitimate ``monkeypatch.setattr(obs_crud, …)`` is already restored
    when this check runs — no false positives. Cost: one ``isinstance`` sweep
    of one small module's namespace per test.

    Caveat: a *session*/*module*-scoped fixture that patches ``obs_crud`` and is
    still active during a later function-scoped test's teardown would trip this
    guard (no such fixture exists today). Use function scope, or set the mock on
    a local object, if you ever need one.
    """
    from unittest.mock import Mock

    import genesis.db.crud.observations as obs_crud

    yield
    leaked = sorted(
        name
        for name, obj in vars(obs_crud).items()
        if not name.startswith("__") and isinstance(obj, Mock)
    )
    if leaked:
        raise AssertionError(
            "Test leaked unittest.mock object(s) onto real module "
            f"genesis.db.crud.observations: {leaked}. Use monkeypatch.setattr "
            "or `with patch(...)` so the patch is restored, or set the mock on a "
            "local mock object — never assign to the real module attribute."
        )


@pytest.fixture
async def empty_db():
    """In-memory SQLite database with tables but no seed data."""
    from genesis.db.connection import SerializedConnection
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(conn)
    await conn.commit()
    wrapped = SerializedConnection(conn)
    yield wrapped
    await wrapped.close()
