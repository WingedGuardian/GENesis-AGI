"""Tests for worktree handling across locate and destructive command guard (#2424).

Covers:
- `locate` pruning custom nested worktrees from `scope="repo"` results while including them in `scope="worktrees"`.
- `locate` searching correctly when `GENESIS_REPO_ROOT` points directly to a linked worktree.
- `locate` running `_list_worktrees` with scrubbed git env so `GIT_DIR` overrides do not redirect worktree discovery.
- `locate` handling non-UTF-8 worktree paths safely via `os.fsdecode`.
- `locate` calling `_list_worktrees` exactly once per `scope="all"` search.
- `destructive_command_guard` applying uniform path depth checks to linked worktrees without exemptions.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from genesis.mcp.memory import locate as locate_mod
from genesis.mcp.memory.locate import _impl_locate, _list_worktrees
from genesis.session_awareness.zero_drop_git import scrubbed_git_env
from scripts.hooks.destructive_command_guard import _check_target


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-b", "main"], check=True, capture_output=True, env=scrubbed_git_env())
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True, env=scrubbed_git_env())
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True, env=scrubbed_git_env())
    readme = path / "README.md"
    readme.write_text("# Main Repo\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, env=scrubbed_git_env())
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial commit"], check=True, env=scrubbed_git_env())


def test_destructive_guard_does_not_exempt_worktrees(tmp_path):
    """Verify shallow linked worktrees (depth < 4) are refused like plain directories."""
    tmp_dir = tmp_path
    repo = tmp_dir / "repo"
    repo.mkdir()
    _init_repo(repo)

    wt_path = tmp_dir / "wt"

    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-branch", str(wt_path)],
        check=True, capture_output=True, env=scrubbed_git_env(),
    )

    plain_dir = tmp_dir / "plain"
    plain_dir.mkdir()

    plain_reason = _check_target(str(plain_dir.relative_to(tmp_dir)))
    assert plain_reason is not None and "too broad" in plain_reason

    wt_reason = _check_target(str(wt_path.relative_to(tmp_dir)))
    assert wt_reason is not None and "too broad" in wt_reason


@pytest.mark.asyncio
async def test_locate_repo_root_that_is_a_linked_worktree(tmp_path, monkeypatch):
    """When GENESIS_REPO_ROOT points to a linked worktree, scope="repo" must still scan it."""
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir()
    _init_repo(main_repo)

    wt_repo = tmp_path / "wt-repo"
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", "-b", "wt-branch", str(wt_repo)],
        check=True, capture_output=True, env=scrubbed_git_env(),
    )

    wt_file = wt_repo / "file_in_wt.md"
    wt_file.write_text("# File in WT\n")

    monkeypatch.setenv("GENESIS_REPO_ROOT", str(wt_repo))

    res_repo = await _impl_locate(scope="repo", within="0")
    names_repo = {r["name"] for r in res_repo["results"]}
    assert "file_in_wt.md" in names_repo


@pytest.mark.asyncio
async def test_locate_prunes_custom_nested_worktree(tmp_path, monkeypatch):
    """Create linked worktree inside main repo at a path outside prescribed directories.

    For example <repo>/elsewhere/feature-1, so scope="repo" would walk into it
    without the fix. Assert worktree_file.md is absent in repo scope and present in worktrees scope.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    wt_path = repo / "elsewhere" / "feature-1"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "feature-1", str(wt_path)],
        check=True, capture_output=True, env=scrubbed_git_env(),
    )

    wt_file = wt_path / "worktree_file.md"
    wt_file.write_text("# Worktree File\n")

    monkeypatch.setenv("GENESIS_REPO_ROOT", str(repo))

    wts = _list_worktrees(repo)
    assert any(wt.resolve() == wt_path.resolve() for wt in wts)

    res_wts = await _impl_locate(scope="worktrees", within="0")
    names_wts = {r["name"] for r in res_wts["results"]}
    assert "worktree_file.md" in names_wts

    res_repo = await _impl_locate(scope="repo", within="0")
    names_repo = {r["name"] for r in res_repo["results"]}
    assert "worktree_file.md" not in names_repo
    assert "README.md" in names_repo


def test_locate_scrubs_git_env_overrides(tmp_path, monkeypatch):
    """Assert GIT_DIR override does not redirect _list_worktrees."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    wt_path = repo / "elsewhere" / "feature-1"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "feature-1", str(wt_path)],
        check=True, capture_output=True, env=scrubbed_git_env(),
    )

    other_repo = tmp_path / "other"
    other_repo.mkdir()
    _init_repo(other_repo)

    monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))

    wts = _list_worktrees(repo)
    assert any(wt.resolve() == wt_path.resolve() for wt in wts)


def test_locate_handles_non_utf8_worktree_paths(tmp_path, monkeypatch):
    """Assert _list_worktrees decodes non-UTF-8 paths using os.fsdecode."""
    raw_bytes = b"worktree /tmp/main\nHEAD aaa\nbranch refs/heads/main\n\nworktree /tmp/caf\xe9\nHEAD bbb\nbranch refs/heads/feature\n\n"
    monkeypatch.setattr(
        locate_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=raw_bytes, stderr=b""),
    )

    wts = _list_worktrees(tmp_path)
    expected_path = Path(os.fsdecode(b"/tmp/caf\xe9"))
    assert wts == [expected_path]


@pytest.mark.asyncio
async def test_locate_scope_all_calls_list_worktrees_once(tmp_path, monkeypatch):
    """Assert scope="all" calls _list_worktrees exactly once."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    monkeypatch.setenv("GENESIS_REPO_ROOT", str(repo))

    call_count = [0]
    orig = locate_mod._list_worktrees

    def spy_list_worktrees(r):
        call_count[0] += 1
        return orig(r)

    monkeypatch.setattr(locate_mod, "_list_worktrees", spy_list_worktrees)

    await _impl_locate(scope="all", within="0")
    assert call_count[0] == 1
