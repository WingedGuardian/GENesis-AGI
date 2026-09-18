"""Commit-side enforcement of the shared distinct-reviewed-head budget."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "review_enforcement_commit.py"
STATE = ROOT / "scripts" / "review_state.py"
CODEX = "chatgpt-codex-connector[bot]"
HEADS = tuple(f"{i:x}" * 40 for i in range(1, 7))

_spec = importlib.util.spec_from_file_location("commit_budget_guard", HOOK)
_guard = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_guard)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "-c", "init.defaultBranch=main", "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "tester")
    (path / "f.py").write_text("value = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    _git(path, "checkout", "-qb", "feature/review-budget")
    (path / "f.py").write_text("value = 2\n")
    _git(path, "add", "-A")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".genesis").mkdir(parents=True)
    return path


def _jsonl(rows) -> str:
    return "\n".join(json.dumps(row) for row in rows)


def _evidence(monkeypatch, reviewed: int, *, gate=False, head=HEADS[4]):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "99")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_REPO", "owner/repo")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", head)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", _jsonl({"sha": sha} for sha in HEADS))
    monkeypatch.setenv(
        "_TEST_GH_CODEX_REVIEWS",
        _jsonl(
            {"login": CODEX, "commit_id": sha, "state": "COMMENTED"} for sha in HEADS[:reviewed]
        ),
    )
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    filename = "scripts/review_budget.py" if gate else "src/example.py"
    monkeypatch.setenv(
        "_TEST_REVIEW_BUDGET_FILES",
        json.dumps({"filename": filename, "previous_filename": None}),
    )


def _mark(repo: Path, home: Path) -> None:
    evidence = home / "review.txt"
    evidence.write_text("adversarial review complete\n" + "specific evidence " * 30)
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        [
            sys.executable,
            str(STATE),
            "mark",
            "--agent-output",
            str(evidence),
            "--source",
            "internal",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run(command: str, repo: Path, home: Path, *, dispatched=False):
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(repo),
            "tool_input": {"command": command},
        }
    )
    env = {**os.environ, "HOME": str(home)}
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    else:
        env.pop("GENESIS_CC_SESSION", None)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _decision(result) -> str:
    if result.returncode == 2:
        return "deny"
    if result.stdout.strip():
        payload = json.loads(result.stdout)
        if payload.get("hookSpecificOutput", {}).get("permissionDecision") == "ask":
            return "ask"
    return "allow"


def test_current_branch_status_resolves_cross_repository_pr_identity():
    raw = json.dumps(
        {
            "currentBranch": {
                "number": 42,
                "state": "OPEN",
                "url": "https://github.example/target/project/pull/42",
            }
        }
    )
    assert _guard._current_branch_pr_identity(raw) == ("target/project", 42)


def test_current_branch_status_distinguishes_no_pr_from_malformed_evidence():
    assert _guard._current_branch_pr_identity('{"createdBy": []}') is None
    malformed = _guard._current_branch_pr_identity(
        '{"currentBranch": {"number": 42, "state": "OPEN", "url": "bad"}}'
    )
    assert isinstance(malformed, dict)
    assert malformed["status"] == "unknown"


def test_cloud_lookup_reserves_two_seconds_for_post_lookup_checks(monkeypatch, repo):
    now = [100.0]
    outer = _guard.Deadline.after(9.5, monotonic=lambda: now[0])
    now[0] += 3.0  # Earlier mandatory local probes consume part of the hook budget.
    calls = []

    def consumes_timeout(*args, timeout, **kwargs):
        calls.append(timeout)
        now[0] += timeout
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "currentBranch": {
                        "number": 99,
                        "state": "OPEN",
                        "url": "https://github.com/owner/repo/pull/99",
                    }
                }
            ),
        )

    monkeypatch.setattr(_guard.subprocess, "run", consumes_timeout)
    result = _guard._branch_review_budget(str(repo), "feature/review-budget", deadline=outer)

    assert result["status"] == "unknown"
    assert calls == [4.5]
    assert outer.remaining() == 2.0


def test_ordinary_four_heads_asks_for_each_commit(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    first = _run('git commit -m "fix"', repo, home)
    second = _run('git commit -m "fix"', repo, home)
    assert _decision(first) == _decision(second) == "ask"
    assert "standing authorization ended after four" in first.stdout


def test_legacy_final_sigil_does_not_replace_native_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run('git commit -m "fix"  # final-round-accept', repo, home)
    assert _decision(result) == "ask"


def test_round_six_commit_is_strongly_discouraged(monkeypatch, repo, home):
    _evidence(monkeypatch, 5, head=HEADS[5])
    _mark(repo, home)
    result = _run('git commit -m "more"', repo, home)
    assert _decision(result) == "ask"
    assert "strongly discouraged" in result.stdout


def test_autonomous_commit_is_denied(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run('git commit -m "fix"', repo, home, dispatched=True)
    assert _decision(result) == "deny"
    assert "standing authorization ended after four" in result.stderr


def test_hard_checks_precede_pending_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    result = _run('git commit --no-verify -m "fix"', repo, home)
    assert _decision(result) == "deny"
    assert "--no-verify" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "one" && git commit --amend -m "two"',
        'git commit -m "one" && git push origin HEAD',
        'git commit -m "one" && gh pr comment 99 --body "@codex review"',
    ],
)
def test_one_approval_cannot_cover_compound_actions(monkeypatch, repo, home, command):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run(command, repo, home)
    assert _decision(result) == "deny"
    assert "exactly one fix commit" in result.stderr


def test_docs_skip_and_review_override_still_pass_through_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _git(repo, "reset", "-q", "HEAD", "--", "f.py")
    (repo / "note.md").write_text("documentation\n")
    _git(repo, "add", "note.md")
    docs = _run('git commit -m "docs"', repo, home)
    assert _decision(docs) == "ask"

    _git(repo, "reset", "-q", "HEAD", "--", "note.md")
    _git(repo, "add", "f.py")
    override = _run('git commit -m "fix"  # review-override', repo, home)
    assert _decision(override) == "ask"


def test_unknown_budget_asks_foreground_and_denies_autonomous(monkeypatch, repo, home):
    _evidence(monkeypatch, 0)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "bad-json")
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"', repo, home)) == "ask"
    assert _decision(_run('git commit -m "fix"', repo, home, dispatched=True)) == "deny"


def test_gate_round_two_fix_is_allowed_but_post_confirmation_fix_asks(monkeypatch, repo, home):
    _evidence(monkeypatch, 2, gate=True, head=HEADS[1])
    _mark(repo, home)
    assert _decision(_run('git commit -m "round two fix"', repo, home)) == "allow"

    _evidence(monkeypatch, 3, gate=True, head=HEADS[2])
    after = _run('git commit -m "post confirmation fix"', repo, home)
    assert _decision(after) == "ask"
    assert "two discovery rounds plus confirmation are spent" in after.stdout


def test_proven_no_open_pr_does_not_invent_a_cloud_round(monkeypatch, repo, home):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "none")
    _mark(repo, home)
    assert _decision(_run('git commit -m "local branch"', repo, home)) == "allow"
