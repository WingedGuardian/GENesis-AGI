"""A task worktree starts from the remote's default branch, not the launching HEAD.

Real git, scratch repositories. The launching checkout is put on a branch that
carries an extra commit (the shape of a local integration branch), and the task
branch must not inherit it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from genesis.autonomy.executor import worktree_mgr


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=str(home / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _out(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


@pytest.fixture()
def git_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "gitconfig").write_text("")
    env = _env(home)
    for k in [k for k in os.environ if k.startswith("GIT_") and k not in env]:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return tmp_path


def _clone_on_extra_branch(root: Path) -> tuple[Path, str, str]:
    """origin with main; a clone checked out on `live` = main + one commit."""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = root / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    _git(seed, "checkout", "-q", "-b", "main")
    (seed / "a.txt").write_text("a\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", "origin", "main")
    repo = root / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    main_sha = _out(repo, "rev-parse", "origin/HEAD")
    _git(repo, "checkout", "-q", "-b", "live")
    (repo / "candidate.txt").write_text("unreviewed\n")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-q", "-m", "candidate")
    live_sha = _out(repo, "rev-parse", "HEAD")
    return repo, main_sha, live_sha


def _solo_repo(root: Path) -> tuple[Path, str]:
    repo = root / "solo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _out(repo, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_resolve_base_reads_origin_head(git_env):
    repo, main_sha, _ = _clone_on_extra_branch(git_env)
    assert await worktree_mgr.resolve_base(repo) == worktree_mgr.BaseRef("main", main_sha)


@pytest.mark.asyncio
async def test_resolve_base_is_none_without_origin_head(git_env):
    repo, _ = _solo_repo(git_env)
    assert await worktree_mgr.resolve_base(repo) is None


@pytest.mark.asyncio
async def test_a_dangling_origin_head_is_unresolved_not_fatal(git_env):
    """git does not update origin/HEAD on fetch. After the remote renames its
    default branch, `fetch --prune` removes the old ref and origin/HEAD still
    names it; cutting from it would fail every task."""
    repo, _, _ = _clone_on_extra_branch(git_env)
    origin = git_env / "origin.git"
    _git(origin, "branch", "-m", "main", "trunk")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
    _git(repo, "fetch", "-q", "--prune", "origin")
    assert _out(repo, "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/main"
    assert _git(repo, "rev-parse", "--verify", "--quiet", "origin/main", check=False).returncode != 0
    assert await worktree_mgr.resolve_base(repo) is None
    wt = await worktree_mgr.create_worktree("abcdef1234", repo, git_env / "wts", base=None)
    assert wt.exists()


@pytest.mark.asyncio
async def test_task_branch_starts_from_the_base_branch_not_the_launching_head(git_env):
    repo, main_sha, live_sha = _clone_on_extra_branch(git_env)
    assert main_sha != live_sha  # guard: the fixture really diverges
    wt = await worktree_mgr.create_worktree(
        "abcdef1234",
        repo,
        git_env / "wts",
        base=worktree_mgr.BaseRef("main", main_sha),
    )
    assert _out(wt, "rev-parse", "HEAD") == main_sha
    assert not (wt / "candidate.txt").exists()


@pytest.mark.asyncio
async def test_task_branch_does_not_track_the_default_branch(git_env):
    """Cut from a remote-tracking ref, a branch TRACKS it unless told not to, and
    then a bare `git pull` / `git push` in the task would target main."""
    repo, main_sha, _ = _clone_on_extra_branch(git_env)
    wt = await worktree_mgr.create_worktree(
        "abcdef1234",
        repo,
        git_env / "wts",
        base=worktree_mgr.BaseRef("main", main_sha),
    )
    res = _git(wt, "rev-parse", "--abbrev-ref", "@{upstream}", check=False)
    assert res.returncode != 0, res.stdout


@pytest.mark.asyncio
async def test_without_a_base_branch_the_task_branch_starts_from_head(git_env):
    """No resolved default branch: keep the previous behaviour instead of failing."""
    repo, head = _solo_repo(git_env)
    wt = await worktree_mgr.create_worktree("fedcba9876", repo, git_env / "wts")
    assert _out(wt, "rev-parse", "HEAD") == head
