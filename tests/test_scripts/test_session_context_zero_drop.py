"""SessionStart spawn seam for the zero-drop detector.

The detector cannot run in-hook: one gh round-trip plus ~160 worktree stats
(MEASURED ~19s) is orders of magnitude past the hook's 5s budget. So the hook's
entire job is one fire-and-forget Popen, and its entire contract is that a
detector failure can never cost anybody a session start.
"""

from __future__ import annotations

import ast
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


def test_the_emission_path_spawns_ONLY_through_the_chokepoint():
    """Enumerate the emission path's spawns — never spot-check a known list.

    The first version of this test iterated a hardcoded tuple of the two
    helpers that existed when it was written, so the one mutant its own
    docstring promised to catch — a THIRD spawn added at the call site — was
    invisible by construction, along with an aliased call
    (`_spawn_zero_drop_worker(src)`) and a commented-out call whose text still
    matched. Asking the AST which `_spawn_*` names `_emit_body` actually calls
    has no such blind spot: a new one shows up without this test being touched.
    """
    tree = ast.parse((_SCRIPTS_DIR / "genesis_session_context.py").read_text())
    emit = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_emit_body"
    )
    spawned = {
        n.func.id
        for n in ast.walk(emit)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id.startswith("_spawn_")
    }
    assert spawned == {"_spawn_boundary_workers"}, (
        f"the emission path spawns outside the chokepoint: {sorted(spawned)} — a test "
        "that stubs the chokepoint would not neutralise those"
    )


def test_the_chokepoint_really_spawns_BOTH_workers(tmp_path, monkeypatch):
    """RUNTIME coverage of the chokepoint, which had none.

    Its predecessor stubbed `_spawn_boundary_workers` and then called it, so it
    asserted that a no-op lambda spawns nothing — vacuously true, and equally
    true if the chokepoint's body were `if False:` or if it spawned ten
    workers. Drive the REAL function and name both workers it must reach.
    """
    calls: list = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(_ctx.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("GENESIS_ZERO_DROP_DISABLED", raising=False)
    monkeypatch.delenv("GENESIS_REPO_PULSE_DISABLED", raising=False)

    _ctx._spawn_boundary_workers("startup")

    assert {Path(argv[1]).name for argv in calls} == {
        "repo_pulse_worker.py",
        "zero_drop_worker.py",
    }, f"the chokepoint did not reach both workers: {calls}"
