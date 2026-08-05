"""Tests for the review escalation cap — the machine-enforced round counter +
commit-gate block (scripts/review_state.py + scripts/review_enforcement_commit.py).

Hermetic: throwaway git repo under ``tmp_path`` with ``HOME`` pointed at another
temp dir, so the real per-worktree markers/round counters under ``~/.genesis/``
are never touched. No network, no live server. (Scratch git in a pytest
subprocess is not blocked by the interactive CC guards — per the hook-testing
convention.)
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

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))
import review_state  # noqa: E402


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


def _stage(repo: Path, content: str) -> None:
    (repo / "f.py").write_text(content)
    _git(repo, "add", "-A")


# ── Counter unit tests (review_state) ─────────────────────────────────────


@pytest.fixture
def _isolate_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(review_state, "_ROUND_DIR", tmp_path / "rounds")


def test_bump_increments_on_distinct_content(repo, _isolate_rounds):
    # Two one-line fixes to the SAME file (identical --stat, different content):
    # the round counter must key on CONTENT, so both count as distinct rounds.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1
    assert review_state.get_review_round(cwd=str(repo)) == 1
    _stage(repo, "a = 3\n")  # same stat, different content → new round
    assert review_state.bump_review_round(cwd=str(repo)) == 2
    assert review_state.get_review_round(cwd=str(repo)) == 2


def test_remark_same_content_no_increment(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1
    # Re-mark the identical staged diff (re-ran /review, no fix) → NOT a new round.
    assert review_state.bump_review_round(cwd=str(repo)) == 1


def test_branch_change_resets(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo))
    _stage(repo, "a = 3\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 2
    _git(repo, "commit", "-qm", "wip")
    _git(repo, "checkout", "-q", "-b", "feature/y")
    _stage(repo, "b = 1\n")
    assert review_state.get_review_round(cwd=str(repo)) == 0  # different branch → fresh
    assert review_state.bump_review_round(cwd=str(repo)) == 1  # reset to 1


def test_get_round_zero_for_other_branch(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo))
    _git(repo, "commit", "-qm", "wip")
    _git(repo, "checkout", "-q", "-b", "other")
    assert review_state.get_review_round(cwd=str(repo)) == 0


def test_reset_clears(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo))
    review_state.reset_review_round(cwd=str(repo))
    assert review_state.get_review_round(cwd=str(repo)) == 0


# ── Gate integration tests (review_enforcement_commit, subprocess) ────────


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


def _reach_rounds(repo: Path, home: Path, n: int) -> None:
    """Drive the round counter to ``n`` via distinct staged diffs + marks."""
    for i in range(1, n + 1):
        _stage(repo, f"a = {i}\n")
        m = _mark(repo, home)
        assert m.returncode == 0, m.stderr


def test_commit_blocked_at_round_cap(repo, home):
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)  # 3 rounds
    # Review IS current for the latest staged diff (just marked), so Rule 2 passes —
    # the escalation cap (Rule 3) is what must now block.
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr


def test_escalation_ack_allows_past_cap(repo, home):
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 0, res.stderr


def test_ack_inside_message_does_not_bypass(repo, home):
    # The token buried in the -m string (not a clean trailing comment) must NOT ack.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    res = _run_hook('git commit -m "escalation-ack please"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr


def test_below_cap_not_blocked_by_escalation(repo, home):
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)  # 2 rounds
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 0, res.stderr  # review current + under cap → allowed


def test_review_override_does_not_bypass_cap(repo, home):
    # The cap is checked BEFORE Rule 2, so a '# review-override' (which would exit
    # the review rule early) can NOT sneak a past-cap commit through.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    _stage(repo, "unreviewed = 1\n")  # make review stale so override would apply
    res = _run_hook('git commit -m "wip"  # review-override', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr


def test_ack_resets_budget_no_permanent_friction(repo, home):
    # Acking is a fresh decision → resets the round budget, so subsequent commits
    # (back under the cap) don't each need a fresh ack.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    r1 = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert r1.returncode == 0, r1.stderr  # ack allows + resets the counter
    _git(repo, "commit", "-qm", "wip")  # actually land it
    _stage(repo, "more = 1\n")
    assert _mark(repo, home).returncode == 0  # one fresh review → round 1
    r2 = _run_hook('git commit -m "wip2"', repo, home)
    assert r2.returncode == 0, r2.stderr  # under cap again, no ack needed


def test_clean_staged_mark_does_not_inflate(repo, _isolate_rounds):
    # A mark with nothing staged ("clean" content hash) is not a review round.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1
    _git(repo, "commit", "-qm", "wip")  # staged area now clean
    assert review_state.bump_review_round(cwd=str(repo)) == 1  # no inflation


def test_docs_only_commit_still_blocked_at_cap(repo, home):
    # The hard stop must NOT be bypassable by file extension: a docs/skill-only
    # commit at the cap (which would otherwise hit the docs-skip) is still blocked.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    _git(repo, "commit", "-qm", "land code")  # clear staging (direct git, no gate)
    (repo / "README.md").write_text("# docs change\n")
    _git(repo, "add", "-A")  # staged set is now docs-only
    res = _run_hook('git commit -m "docs"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr
    # ...and an ack lets the docs commit through.
    res2 = _run_hook('git commit -m "docs"  # escalation-ack', repo, home)
    assert res2.returncode == 0, res2.stderr


def test_corrupt_round_counter_does_not_crash(repo, _isolate_rounds):
    # A non-integer 'round' (corrupt / partial write / version skew) must NOT raise
    # from bump — marking a review must always succeed (best-effort counter).
    import json

    _stage(repo, "a = 2\n")
    rf = review_state._round_file(cwd=str(repo))
    rf.parent.mkdir(parents=True, exist_ok=True)
    branch = review_state.get_current_branch(cwd=str(repo))
    rf.write_text(json.dumps({"branch": branch, "round": "not-an-int", "last_hash": "old"}))
    result = review_state.bump_review_round(cwd=str(repo))  # must not raise
    assert result == 1  # coerced the bad value to 0, then +1
