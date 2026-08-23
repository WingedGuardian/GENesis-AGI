"""Tests for git_discard_guard — blocks git commands that discard uncommitted work.

Hermetic: each test builds a real throwaway git repo under tmp_path (git in a
pytest subprocess is fine — the CC Bash guards police scratch git in the Bash
TOOL, not pytest subprocesses). The guard's "ask git, don't parse git" design is
exercised against real `git status` output, so a branch name is never mistaken
for a dirty path.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS = _WORKTREE / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_discard_guard", _HOOKS / "git_discard_guard.py")
_gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gd)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",  # hermetic — ignore the user's ~/.gitconfig
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "tracked.py").write_text("orig\n")
    (r / "keep.py").write_text("keep\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    _git(r, "branch", "feature")  # a real branch that is NOT a dirty path
    return r


def _blocks(cmd: str, repo: Path) -> bool:
    """True iff the guard would block *cmd* run in *repo*."""
    return bool(_gd._violations(cmd, {"cwd": str(repo)}))


# ── checkout ────────────────────────────────────────────────────────────────
def test_checkout_dirty_tracked_path_blocks(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert _blocks("git checkout tracked.py", repo)
    assert _blocks("git checkout -- tracked.py", repo)


def test_checkout_clean_path_allows(repo):
    assert not _blocks("git checkout keep.py", repo)  # keep.py unmodified


def test_checkout_branch_switch_allows_even_with_dirty_tree(repo):
    (repo / "tracked.py").write_text("changed\n")  # dirty tree
    # 'feature' is a branch, NOT a dirty path → git status -- feature is empty.
    assert not _blocks("git checkout feature", repo)


def test_checkout_new_branch_allows(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert not _blocks("git checkout -b newbranch", repo)


def test_checkout_untracked_file_allows(repo):
    (repo / "untracked.py").write_text("new\n")  # untracked → column '?'
    assert not _blocks("git checkout untracked.py", repo)


# ── restore ─────────────────────────────────────────────────────────────────
def test_restore_dirty_path_blocks(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert _blocks("git restore tracked.py", repo)


def test_restore_staged_only_allows(repo):
    (repo / "tracked.py").write_text("changed\n")
    _git(repo, "add", "tracked.py")
    # --staged only touches the index, not the worktree → no worktree loss.
    assert not _blocks("git restore --staged tracked.py", repo)


def test_restore_explicit_worktree_blocks(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert _blocks("git restore --worktree tracked.py", repo)


# ── reset --hard (UNCONDITIONAL — discard-intent, blocked regardless of tree) ──
def test_reset_hard_dirty_tree_blocks(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert _blocks("git reset --hard", repo)
    assert _blocks("git reset --hard HEAD", repo)


def test_reset_hard_clean_tree_still_blocks(repo):
    # reset --hard is discard-intent; blocked even on a clean tree (deterministic,
    # matches the prior crude guard). The escape is `# discard-override`.
    assert _blocks("git reset --hard", repo)


def test_reset_soft_allows(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert not _blocks("git reset --soft HEAD~1", repo)  # keeps worktree


def test_reset_mixed_allows(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert not _blocks("git reset HEAD~1", repo)  # default mixed — keeps worktree


# ── clean ────────────────────────────────────────────────────────────────────
def test_clean_force_with_untracked_blocks(repo):
    (repo / "junk.tmp").write_text("junk\n")
    assert _blocks("git clean -f", repo)


def test_clean_cluster_spelling_blocks(repo):
    """The bypass the old substring guard missed: -xf / -fdx clusters."""
    (repo / "junk.tmp").write_text("junk\n")
    assert _blocks("git clean -xf", repo)
    assert _blocks("git clean -fdx", repo)


def test_clean_long_force_blocks(repo):
    (repo / "junk.tmp").write_text("junk\n")
    assert _blocks("git clean --force", repo)


def test_clean_force_blocks_even_with_nothing_to_remove(repo):
    # clean -f is delete-intent; blocked unconditionally (deterministic, matches
    # the prior crude guard). Override escapes. A dry-run (-n, no force) is allowed.
    assert _blocks("git clean -f", repo)


def test_clean_dry_run_allows(repo):
    (repo / "junk.tmp").write_text("junk\n")
    assert not _blocks("git clean -n", repo)  # no force flag → dry run, deletes nothing
    assert not _blocks("git clean -nd", repo)


# ── override + fail-open ─────────────────────────────────────────────────────
def test_discard_override_allows(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert not _blocks("git checkout tracked.py  # discard-override", repo)
    assert not _blocks("git reset --hard  # discard-override", repo)


def test_non_repo_cwd_fails_open(tmp_path):
    (tmp_path / "tracked.py").write_text("x\n")
    assert not _gd._violations("git checkout tracked.py", {"cwd": str(tmp_path)})


def test_unknown_cwd_fails_open(repo):
    (repo / "tracked.py").write_text("changed\n")
    # No cwd in payload → cannot locate repo → allow (fail-open).
    assert not _gd._violations("git checkout tracked.py", {})


def test_unrelated_git_command_ignored(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert not _blocks("git status", repo)
    assert not _blocks("git log --oneline", repo)


# ── M1 regression: git global value-flags must not bypass the subcommand parse ──
def test_git_dir_global_flag_reset_hard_blocks(repo):
    # `git --git-dir X reset --hard` — the separated global value-flag must not
    # mask the subcommand (the hand-rolled parser bypass the architect caught).
    assert _gd._violations("git --git-dir /x reset --hard", {"cwd": str(repo)})


def test_namespace_global_flag_reset_hard_blocks(repo):
    assert _gd._violations("git --namespace ns reset --hard", {"cwd": str(repo)})


def test_work_tree_global_flag_checkout_dirty_blocks(repo):
    (repo / "tracked.py").write_text("changed\n")
    assert _gd._violations(f"git --work-tree {repo} checkout tracked.py", {"cwd": str(repo)})


# ── cwd resolution: git -C and cd-chain ──────────────────────────────────────
def test_git_dash_C_resolves_repo(repo, tmp_path):
    (repo / "tracked.py").write_text("changed\n")
    # Command run from elsewhere but targeting the repo via -C.
    assert _gd._violations(f"git -C {repo} checkout tracked.py", {"cwd": str(tmp_path)})


def test_cd_chain_resolves_repo(repo, tmp_path):
    (repo / "tracked.py").write_text("changed\n")
    assert _gd._violations(f"cd {repo} && git checkout tracked.py", {"cwd": str(tmp_path)})


# ── main() exit codes (payload → stdin) ──────────────────────────────────────
def test_main_blocks_with_exit_2(repo, monkeypatch):
    (repo / "tracked.py").write_text("changed\n")
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "git checkout tracked.py"}, "cwd": str(repo)},
    )
    assert _gd.main() == 2


def test_main_allows_with_exit_0(repo, monkeypatch):
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "git checkout feature"}, "cwd": str(repo)},
    )
    assert _gd.main() == 0


def test_main_ignores_non_git(monkeypatch):
    monkeypatch.setattr(
        _gd,
        "read_payload",
        lambda: {"tool_input": {"command": "ls -la"}, "cwd": "/tmp"},
    )
    assert _gd.main() == 0
