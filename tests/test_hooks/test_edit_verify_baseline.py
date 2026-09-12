"""Unit tests for edit_verify_advisory's baseline gating — the git-error branches.

The black-box subprocess tests in test_edit_verify_advisory.py cover the happy
paths; these monkeypatch subprocess.run to exercise branches that are hard to
trigger for real: a benign "no committed baseline" exit (safe to format), and a
FATAL git exit — corrupt/locked repo — which must be treated as UNSAFE (never
reflow a tracked file we merely failed to read). Codex P2 (#1249).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS))
import edit_verify_advisory as eva  # noqa: E402

_DUMMY_RUFF = Path("/nonexistent/ruff")


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, result) -> None:
    def fake_run(*_a, **_k):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(eva.subprocess, "run", fake_run)


def test_committed_baseline_returns_text(monkeypatch):
    _patch_run(monkeypatch, _Proc(0, stdout="x = 1\n"))
    baseline, ok = eva._git_baseline(Path("/repo/f.py"))
    assert ok is True and baseline == "x = 1\n"


def test_benign_new_file_is_safe(monkeypatch):
    _patch_run(monkeypatch, _Proc(128, stderr="fatal: path 'f.py' does not exist in 'HEAD'"))
    assert eva._git_baseline(Path("/repo/f.py")) == (None, True)
    assert eva._baseline_status(_DUMMY_RUFF, Path("/repo/f.py")) == (True, True)


def test_not_a_repo_is_safe(monkeypatch):
    _patch_run(
        monkeypatch,
        _Proc(128, stderr="fatal: not a git repository (or any of the parent directories): .git"),
    )
    assert eva._git_baseline(Path("/tmp/f.py")) == (None, True)


def test_unborn_head_is_safe(monkeypatch):
    _patch_run(monkeypatch, _Proc(128, stderr="fatal: bad revision 'HEAD'"))
    assert eva._git_baseline(Path("/repo/f.py")) == (None, True)


def test_fatal_repo_error_is_unsafe(monkeypatch):
    """A corrupt/locked repo exits non-zero with an UNrecognised fatal message —
    a tracked file may exist but be unreadable, so do NOT reflow."""
    _patch_run(monkeypatch, _Proc(128, stderr="error: object file .git/objects/ab/cd is empty"))
    assert eva._git_baseline(Path("/repo/f.py")) == (None, False)
    assert eva._baseline_status(_DUMMY_RUFF, Path("/repo/f.py")) == (False, False)


def test_git_binary_missing_is_unsafe(monkeypatch):
    _patch_run(monkeypatch, FileNotFoundError("git not found"))
    assert eva._git_baseline(Path("/repo/f.py")) == (None, False)
    assert eva._baseline_status(_DUMMY_RUFF, Path("/repo/f.py")) == (False, False)
