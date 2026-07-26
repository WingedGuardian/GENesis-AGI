"""Tests for scripts/review_enforcement_commit.py — the commit review gate.

Focus (post-#1227 hardening):
- The gate is *satisfiable without gstack*: ``review_state.py mark`` writes a
  marker on agent-output evidence alone (gstack skill-usage telemetry is
  advisory, not required — it is absent on most hosts).
- The ``# review-override`` token bypasses ONLY the review rule, must be a
  genuine trailing shell comment (outside quotes), and denies-with-explanation
  when buried in the commit message (where it would leak into public history).

Install-agnostic: builds a throwaway git repo under ``tmp_path`` and points
``HOME`` at another temp dir, so the real ``~/.genesis/review_state.json`` and
any concurrent session's markers are never touched. No network, no live server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _REPO_ROOT / "scripts" / "review_enforcement_commit.py"
_REVIEW_STATE = _REPO_ROOT / "scripts" / "review_state.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo on a feature branch with a staged (unreviewed) change."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "f.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/x")
    (r / "f.txt").write_text("changed\n")  # staged below → has_code_changes
    _git(r, "add", "-A")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _run_hook(command: str, repo: Path, home: Path) -> subprocess.CompletedProcess:
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "test",
        }
    )
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _mark(repo: Path, home: Path) -> subprocess.CompletedProcess:
    """Run `review_state.py mark` with fresh agent-output evidence, no gstack."""
    (home / ".genesis" / "last_code_review.txt").write_text("adversarial review: OK\n")
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [
            sys.executable,
            str(_REVIEW_STATE),
            "mark",
            "--agent-output",
            str(home / ".genesis" / "last_code_review.txt"),
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── Rule 2: review required ──────────────────────────────────────────────


def test_plain_commit_blocked_without_review(repo: Path, home: Path) -> None:
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2
    assert "BLOCKED" in res.stderr
    assert "without review" in res.stderr


def test_trailing_override_allows(repo: Path, home: Path) -> None:
    res = _run_hook('git commit -m "wip"  # review-override', repo, home)
    assert res.returncode == 0, res.stderr
    assert "review-override honored" in res.stderr


def test_override_inside_message_denied(repo: Path, home: Path) -> None:
    """Token buried in the -m string would leak into history → deny + explain."""
    res = _run_hook('git commit -m "wip # review-override"', repo, home)
    assert res.returncode == 2
    assert "not a clean trailing shell" in res.stderr


def test_override_jammed_into_unquoted_word_denied(repo: Path, home: Path) -> None:
    """`-m x#review-override` (no space) → the # is literal in the message."""
    res = _run_hook("git commit -m x#review-override", repo, home)
    assert res.returncode == 2
    assert "not a clean trailing shell" in res.stderr


def test_override_with_single_quoted_message(repo: Path, home: Path) -> None:
    """Trailing override after a single-quoted message is honored."""
    res = _run_hook("git commit -m 'wip work'  # review-override", repo, home)
    assert res.returncode == 0, res.stderr
    assert "review-override honored" in res.stderr


def test_override_with_trailing_text(repo: Path, home: Path) -> None:
    """A trailing comment may carry text after the sigil (it's commented out)."""
    res = _run_hook('git commit -m "wip"  # review-override: accepted P2s', repo, home)
    assert res.returncode == 0, res.stderr
    assert "review-override honored" in res.stderr


def test_override_on_add_commit_chain(repo: Path, home: Path) -> None:
    """The git-add-&&-commit path (marker-based Rule 2) also honors override."""
    _git(repo, "reset", "-q")  # unstage so nothing is staged yet
    (repo / "g.txt").write_text("new\n")
    res = _run_hook('git add -A && git commit -m "wip"  # review-override', repo, home)
    assert res.returncode == 0, res.stderr
    assert "review-override honored" in res.stderr


# ── Override never defeats Rule 0 (--no-verify) or Rule 1 (main) ─────────


def test_no_verify_long_not_overridable(repo: Path, home: Path) -> None:
    res = _run_hook('git commit --no-verify -m "wip"  # review-override', repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_short_bundled_not_overridable(repo: Path, home: Path) -> None:
    """The real hole: -nm is --no-verify + -m. Override must not slip it through."""
    res = _run_hook('git commit -nm "wip"  # review-override', repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_short_standalone(repo: Path, home: Path) -> None:
    res = _run_hook('git commit -n -m "wip"', repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_mentioned_in_message_not_blocked(repo: Path, home: Path) -> None:
    """A '--no-verify' inside the commit message must NOT trip Rule 0."""
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -m "document --no-verify behavior"', repo, home)
    assert res.returncode == 0, res.stderr


def test_main_branch_not_overridable(repo: Path, home: Path) -> None:
    _git(repo, "checkout", "-q", "main")
    (repo / "f.txt").write_text("main-change\n")
    _git(repo, "add", "-A")
    res = _run_hook('git commit -m "wip"  # review-override', repo, home)
    assert res.returncode == 2
    assert "main" in res.stderr.lower()


# ── B1: the gate is satisfiable without gstack ──────────────────────────


def test_mark_succeeds_without_gstack(repo: Path, home: Path) -> None:
    assert not (home / ".gstack").exists()  # gstack genuinely absent
    res = _mark(repo, home)
    assert res.returncode == 0, res.stderr
    assert "Review marker written" in res.stdout
    marker = json.loads((home / ".genesis" / "review_state.json").read_text())
    assert "authoritative" in marker["review_evidence"]  # advisory annotation


def test_mark_refuses_without_agent_output(repo: Path, home: Path) -> None:
    """Authoritative evidence (agent output) is still mandatory."""
    env = {**os.environ, "HOME": str(home)}
    res = subprocess.run(
        [
            sys.executable,
            str(_REVIEW_STATE),
            "mark",
            "--agent-output",
            str(home / ".genesis" / "does_not_exist.txt"),
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 1
    assert "REFUSED" in res.stderr


def test_commit_allowed_after_marking(repo: Path, home: Path) -> None:
    """End-to-end: mark (no gstack) → the same staged commit is allowed."""
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 0, res.stderr
