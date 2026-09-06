"""SessionStart spawn seam for the zero-drop detector.

The detector cannot run in-hook: one gh round-trip plus ~160 worktree stats
(MEASURED ~19s) is orders of magnitude past the hook's 5s budget. So the hook's
entire job is one fire-and-forget Popen, and its entire contract is that a
detector failure can never cost anybody a session start.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_ctx_spec = importlib.util.spec_from_file_location(
    "genesis_session_context_zero_drop", _SCRIPTS_DIR / "genesis_session_context.py"
)
_ctx = importlib.util.module_from_spec(_ctx_spec)
_ctx_spec.loader.exec_module(_ctx)


def test_spawn_invokes_the_worker_with_the_home_anchored_db(tmp_path, monkeypatch):
    """The DB path is passed EXPLICITLY: a worktree session's worker would
    otherwise fall back to genesis.env's repo-anchored default, and
    <worktree>/data/ is a void — the sweep would write its findings into a
    throwaway database and read as silently working."""
    calls: list[dict] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: calls.append({"argv": argv, **kw}))
    monkeypatch.setattr(_ctx.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("GENESIS_ZERO_DROP_DISABLED", raising=False)

    _ctx._spawn_zero_drop_worker("startup")

    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[1].endswith("scripts/zero_drop_worker.py")
    assert argv[argv.index("--trigger") + 1] == "session_start"
    assert argv[argv.index("--db-path") + 1] == str(_ctx._charter_db_path())
    assert calls[0]["start_new_session"] is True
    assert (tmp_path / ".genesis" / "session_awareness").exists()


def test_spawn_skipped_on_clear_and_by_the_kill_switch(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(_ctx.Path, "home", staticmethod(lambda: tmp_path))

    monkeypatch.delenv("GENESIS_ZERO_DROP_DISABLED", raising=False)
    _ctx._spawn_zero_drop_worker("clear")  # /clear is a fresh start, not a boundary
    monkeypatch.setenv("GENESIS_ZERO_DROP_DISABLED", "1")
    _ctx._spawn_zero_drop_worker("startup")

    assert calls == []


def test_spawn_failure_never_raises(monkeypatch):
    """Fail-open end to end. A detector is advisory; session start is not."""

    def boom(*a, **kw):
        raise OSError("no fds left")

    monkeypatch.setattr("subprocess.Popen", boom)
    monkeypatch.delenv("GENESIS_ZERO_DROP_DISABLED", raising=False)
    _ctx._spawn_zero_drop_worker("startup")


def test_the_charter_part_actually_calls_the_spawn(monkeypatch):
    """Built != wired. The seam is only worth anything if the emission path
    reaches it — a spawn helper nobody calls is the exact silent no-op this
    subsystem exists to detect."""
    source = (_SCRIPTS_DIR / "genesis_session_context.py").read_text()
    assert source.count("_spawn_zero_drop_worker(") == 2, (
        "expected exactly one definition and one call site"
    )
    assert "_spawn_zero_drop_worker(_hook_source)" in source
