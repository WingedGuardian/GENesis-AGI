"""``hooks.asks.push_pr_branch: off`` silences one push shape: onto an open PR's branch.

Owner ruling 2026-10-10: a session updates a pull request's branch from a
scratch checkout with ``git push <remote> <cur>:refs/heads/<dst>`` when that
branch is checked out in another worktree. The guard always asked about it. The
install may now silence it under its OWN key, so it can be switched back on
without touching ``push_routine``. Silenced only when ``<dst>`` has an open PR
on this repository, the remote is an owned GitHub repo, ``<dst>`` is not a
default branch, and nothing else in the command can change config first.

Same shape as the other ask keys: NO permission decision plus a context note.
Real git repos and remotes; the PR lookup and network probes are stubbed.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from tests.conftest import private_module

gpg = private_module(
    "git_push_guard",
    Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py",
)

PUBLIC = "https://github.com/owner/repo"
STRANGER = "https://github.com/stranger/repo"
PUSH = "git push origin HEAD:refs/heads/feat/pr-branch"


def _repo(tmp_path, git_config=(), git_cmds=()) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["checkout", "--quiet", "-b", "close-scratch"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    for kv in git_config:
        subprocess.run(
            ["git", "-C", str(repo), "config", *kv], capture_output=True, timeout=30, check=True
        )
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "seed"],
        capture_output=True,
        timeout=30,
    )
    for args in git_cmds:
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30, check=True)
    return repo


def _run(
    monkeypatch,
    tmp_path,
    capsys,
    command,
    git_config=(("remote.origin.url", PUBLIC),),
    *,
    git_cmds=(),
):
    repo = _repo(tmp_path, git_config, git_cmds)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.chdir(repo)
    rc = gpg.main()
    out = capsys.readouterr()
    return rc, out.out, out.err


def _hso(out: str) -> dict:
    return json.loads(out)["hookSpecificOutput"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Foreground, no network; the destination branch has one open PR."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    monkeypatch.setattr(gpg, "_remote_branch_definitely_absent", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    for var in gpg._TRANSPORT_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_pr_branch=off")


def _assert_silenced(rc, out, err):
    assert rc == 0, (rc, out, err)
    hso = _hso(out)
    assert "permissionDecision" not in hso, out
    assert "hooks.asks.push_pr_branch: off" in hso["additionalContext"], out
    assert "feat/pr-branch" in hso["additionalContext"], out


def _assert_asks(rc, out, err):
    assert rc == 0, (rc, out, err)
    assert _hso(out).get("permissionDecision") == "ask", (out, err)


# ─── silenced ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        PUSH,
        "git push origin close-scratch:refs/heads/feat/pr-branch",
        "git push origin refs/heads/close-scratch:refs/heads/feat/pr-branch",
    ],
)
def test_push_onto_an_open_pr_branch_is_silenced(monkeypatch, tmp_path, capsys, off, command):
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, command))


def test_the_pr_lookup_is_asked_about_the_destination_branch(monkeypatch, tmp_path, capsys, off):
    seen = []

    def lookup(branch, *a, **k):
        seen.append(branch)
        return 1

    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lookup)
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, PUSH))
    assert seen == ["feat/pr-branch"]


# ─── still asks ──────────────────────────────────────────────────────────────


def test_key_on_by_default_asks_exactly_as_before(monkeypatch, tmp_path, capsys):
    rc, out, err = _run(monkeypatch, tmp_path, capsys, PUSH)
    _assert_asks(rc, out, err)
    assert _hso(out)["permissionDecisionReason"] == (
        "git push needs your approval before publishing externally "
        "(target: refs/heads/feat/pr-branch)."
    )


def test_push_routine_off_does_not_reach_it(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_routine=off")
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, PUSH))


@pytest.mark.parametrize("open_prs", [0, None])
def test_no_open_pr_or_an_unanswerable_lookup_asks(monkeypatch, tmp_path, capsys, off, open_prs):
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: open_prs)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, PUSH)
    _assert_asks(rc, out, err)
    assert (
        "push_pr_branch is off, but this push did not qualify"
        in _hso(out)["permissionDecisionReason"]
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD:refs/heads/main",  # the default branch
        "git push origin HEAD:feat/pr-branch",  # unqualified destination
        "git push origin :refs/heads/feat/pr-branch",  # delete
        "git push origin HEAD:refs/tags/feat/pr-branch",  # tag
        "git push origin HEAD:refs/heads/feat/pr-branch HEAD:refs/heads/other",
        "git push origin main:refs/heads/feat/pr-branch",  # source is not the current branch
        "git push origin @{u}:refs/heads/feat/pr-branch",
        "git push origin HEAD~1:refs/heads/feat/pr-branch",
        "git push --all origin",
        "git push -c core.sshCommand=x origin HEAD:refs/heads/feat/pr-branch",
        "GIT_CONFIG_COUNT=1 " + PUSH,
        "git config remote.origin.url https://github.com/stranger/repo && " + PUSH,
        "git checkout main && " + PUSH,
    ],
)
def test_other_shapes_keep_asking(monkeypatch, tmp_path, capsys, off, command):
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, command))


@pytest.mark.parametrize(
    "command",
    [
        "git push --force origin HEAD:refs/heads/feat/pr-branch",
        "git push origin +HEAD:refs/heads/feat/pr-branch",
    ],
)
def test_a_force_is_blocked_not_silenced(monkeypatch, tmp_path, capsys, off, command):
    rc, _out, err = _run(monkeypatch, tmp_path, capsys, command)
    assert rc == 2
    assert "BLOCKED" in err


def test_a_stranger_remote_asks(monkeypatch, tmp_path, capsys, off):
    _assert_asks(
        *_run(monkeypatch, tmp_path, capsys, PUSH, git_config=(("remote.origin.url", STRANGER),))
    )


def test_a_recorded_default_branch_asks(monkeypatch, tmp_path, capsys, off):
    _assert_asks(
        *_run(
            monkeypatch,
            tmp_path,
            capsys,
            PUSH,
            git_cmds=(
                ["update-ref", "refs/remotes/origin/feat/pr-branch", "HEAD"],
                ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/feat/pr-branch"],
            ),
        )
    )


def test_a_detached_head_asks(monkeypatch, tmp_path, capsys, off):
    _assert_asks(
        *_run(monkeypatch, tmp_path, capsys, PUSH, git_cmds=(["checkout", "--quiet", "--detach"],))
    )


def test_a_dispatched_session_is_still_denied(monkeypatch, tmp_path, capsys, off):
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: True)
    rc, _out, err = _run(monkeypatch, tmp_path, capsys, PUSH)
    assert rc == 2
    assert "BLOCKED" in err
