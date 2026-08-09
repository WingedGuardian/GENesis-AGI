"""Integration tests for the commit-gate review-DEPTH rule (Rule 2.5).

Hermetic: throwaway git repo under ``tmp_path`` with ``HOME`` redirected, so real
~/.genesis markers are untouched. Drives scripts/review_enforcement_commit.py as a
subprocess exactly as the CC PreToolUse hook does (JSON payload on stdin).

The rule: a SUBSTANTIAL staged change requires an ADVERSARIAL review marker; a
shallow/inline pass BLOCKS (exit 2). '# review-override' waives FINDINGS but NOT
depth (D1); '# depth-ack' is the audited escape. Inline changes are unaffected.
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

# A review artifact with adversarial-audit structure: a severity ladder + a
# file:line pointer + past the length floor.
_ADVERSARIAL = (
    "Scope Check: CLEAN\n"
    "BLOCKER 1 — off-by-one at f.py:1 mishandles the empty case.\n"
    "SHOULD-FIX 2 — missing boundary validation.\n"
    "NOTE 3 — consider a test for the None input.\n"
    "Completion status: DONE.\n" + "detail " * 80
)
_SHALLOW = "Reviewed the change. Looks good to me. 88% confident.\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "f.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/x")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _stage_substantial(repo: Path) -> None:
    (repo / "f.py").write_text("base = 1\n" + "".join(f"x{i} = {i}\n" for i in range(60)))
    _git(repo, "add", "-A")


def _stage_inline(repo: Path) -> None:
    (repo / "f.py").write_text("base = 2\n")
    _git(repo, "add", "-A")


def _mark(repo: Path, home: Path, evidence: str) -> subprocess.CompletedProcess:
    """Write review evidence then run `review_state.py mark` (computes level/adversarial)."""
    (home / ".genesis" / "last_code_review.txt").write_text(evidence)
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


# --------------------------------------------------------------------------- #
def test_substantial_without_marker_blocked_on_depth(repo, home):
    _stage_substantial(repo)
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


def test_substantial_with_shallow_marker_blocked(repo, home):
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)  # marker: level=substantial, adversarial=False
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


def test_substantial_with_adversarial_marker_allowed(repo, home):
    _stage_substantial(repo)
    m = _mark(repo, home, _ADVERSARIAL)
    assert m.returncode == 0
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 0, r.stderr


def test_inline_with_shallow_marker_allowed(repo, home):
    # A small inline change never triggers the depth requirement.
    _stage_inline(repo)
    _mark(repo, home, _SHALLOW)
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 0, r.stderr


def test_depth_ack_allows_substantial_shallow(repo, home):
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)
    r = _run_hook('git commit -m "x"  # depth-ack', repo, home)
    assert r.returncode == 0, r.stderr
    assert "depth-ack honored" in r.stderr.lower()


def test_review_override_does_not_waive_depth(repo, home):
    # D1: '# review-override' waives FINDINGS but must NOT waive the depth requirement.
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)
    r = _run_hook('git commit -m "x"  # review-override', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


def test_depth_ack_inside_quotes_does_not_waive(repo, home):
    # '# depth-ack' inside the commit MESSAGE (quoted) is not a clean trailing shell
    # comment, so it must NOT waive depth — mirrors the other sigils' in-quote handling.
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)
    r = _run_hook('git commit -m "fix bug  # depth-ack"', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


def test_add_and_commit_chain_falls_back_to_marker_level(repo, home):
    # `git add && commit` in ONE command: the index is EMPTY at hook time, so the
    # current-staged classify reads "inline" — depth must fall back to the marker's
    # RECORDED substantial level, or a substantial change would slip the gate here.
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)  # marker.level = substantial, adversarial = False
    _git(repo, "reset", "-q")  # empty the index → classify(cwd) would see "inline"
    r = _run_hook('git add -A && git commit -m "x"', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()
