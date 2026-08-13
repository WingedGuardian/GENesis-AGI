"""Concurrent-session isolation for the review gate's per-worktree state.

#1244 made the review MARKER and ROUND counter per-worktree, but two gaps remained
and are closed here:

1. ``_worktree_key`` fell back to the SHARED constant ``"default"`` whenever
   ``git rev-parse --show-toplevel`` failed (timeout/error). Under concurrent load
   — exactly when a 5s git call is most likely to hiccup — every session collapsed
   onto the same key and clobbered each other's marker/round/evidence again. The
   fallback must stay per-location (derive the worktree root without git), never a
   single shared bucket.

2. The review-EVIDENCE file (``--agent-output``) defaulted to a single global
   ``~/.genesis/last_code_review.txt``; concurrent sessions overwrote each other's
   evidence, and the depth gate validates the marker against whatever content is in
   that file. The default is now per-worktree.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("review_state", _SCRIPTS / "review_state.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["review_state"] = mod
    spec.loader.exec_module(mod)
    return mod


_rs = _load()


def _mk_worktree(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".git").write_text("gitdir: /nowhere\n")  # a linked-worktree .git FILE
    return root


def test_worktree_key_never_collapses_to_shared_default_on_git_failure(tmp_path, monkeypatch):
    a = _mk_worktree(tmp_path, "wt_a")
    b = _mk_worktree(tmp_path, "wt_b")

    # Force the git path to fail so the FALLBACK is exercised.
    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(_rs.subprocess, "run", _boom)

    ka = _rs._worktree_key(cwd=str(a))
    kb = _rs._worktree_key(cwd=str(b))

    # The whole point: two worktrees must NOT share a key even when git fails.
    assert ka != "default", "fallback must not be the shared constant"
    assert kb != "default"
    assert ka != kb, "two worktrees collapsed onto the same fallback key"
    # Stable: same cwd -> same key across calls.
    assert ka == _rs._worktree_key(cwd=str(a))


def test_evidence_file_is_per_worktree(tmp_path, monkeypatch):
    a = _mk_worktree(tmp_path, "wt_a")
    b = _mk_worktree(tmp_path, "wt_b")

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(_rs.subprocess, "run", _boom)

    ea = _rs._evidence_file(cwd=str(a))
    eb = _rs._evidence_file(cwd=str(b))
    assert ea != eb, "evidence files collide across worktrees"
    # And distinct from the legacy global path.
    assert ea != Path.home() / ".genesis" / "last_code_review.txt"


def test_worktree_key_success_and_fallback_agree(tmp_path, monkeypatch):
    # THE core correctness property: for the SAME worktree, the git-success key must
    # equal the git-FAILURE fallback key — else a git-success `mark` and a git-failed
    # commit-hook check compute different keys and the gate falsely says "no review".
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    k_git = _rs._worktree_key(cwd=str(repo))  # git rev-parse succeeds

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(_rs.subprocess, "run", _boom)
    k_fallback = _rs._worktree_key(cwd=str(repo))  # forced onto the walk-up fallback
    assert k_git == k_fallback, "git-success and fallback keys diverge for one worktree"


def test_worktree_key_subdir_matches_root_on_git_failure(tmp_path, monkeypatch):
    # A cwd deep inside the worktree must key to the SAME worktree root as the root
    # itself (the walk-up climbs to .git), matching what git --show-toplevel returns.
    repo = tmp_path / "repo"
    (repo / "sub" / "deep").mkdir(parents=True)
    (repo / ".git").mkdir()

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(_rs.subprocess, "run", _boom)
    assert _rs._worktree_key(cwd=str(repo / "sub" / "deep")) == _rs._worktree_key(cwd=str(repo))


def test_mark_reviewed_default_wires_the_per_worktree_evidence_file(tmp_path, monkeypatch):
    # Guards the actual wiring (mark_reviewed's default agent_path = _evidence_file(cwd)),
    # not just the path shape — a regression hardcoding the global path would be caught.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    captured = {}

    def _capture(path):
        captured["path"] = path
        return (False, "stub")  # short-circuit mark_reviewed right after capture

    monkeypatch.setattr(_rs, "_verify_agent_output", _capture)
    _rs.mark_reviewed(agent_output_path=None, cwd=str(repo))
    assert captured["path"] == str(_rs._evidence_file(cwd=str(repo)))


def test_evidence_path_cli_creates_the_dir(tmp_path, monkeypatch, capsys):
    # The evidence-path handler must create _EVIDENCE_DIR so a `> $(evidence-path)`
    # shell redirect doesn't fail on a fresh host (which would then wedge the gate).
    ev_dir = tmp_path / "review_evidence"
    monkeypatch.setattr(_rs, "_EVIDENCE_DIR", ev_dir)
    monkeypatch.setattr(sys, "argv", ["review_state.py", "evidence-path"])
    assert not ev_dir.exists()
    _rs.main()
    assert ev_dir.exists(), "evidence-path must create _EVIDENCE_DIR"
    assert capsys.readouterr().out.strip().startswith(str(ev_dir))
