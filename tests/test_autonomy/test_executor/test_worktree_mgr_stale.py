"""Re-creating a task worktree never deletes work (#2926 PR 4).

Real git, scratch repositories. Before this change a stale task worktree that
``git worktree remove`` refused (uncommitted changes) was ``rmtree``d, and the
re-create then failed anyway because the branch and registration survived
(MEASURED). Now a registered worktree is left for the reaper with a clear
error, and an orphan directory git does not know goes to the trash.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from genesis.autonomy.executor import worktree_mgr
from genesis.trash import ITEM, list_entries


@pytest.fixture()
def repo(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "gitconfig").write_text("")
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{k}_NAME", "t")
        monkeypatch.setenv(f"{k}_EMAIL", "t@example.invalid")
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return r


TASK = "abcdef1234567890"


@pytest.mark.asyncio
async def test_a_dirty_registered_worktree_is_left_in_place(repo):
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    (wt / "work.txt").write_text("uncommitted task work")
    subprocess.run(["git", "-C", str(wt), "add", "work.txt"], check=True)

    with pytest.raises(worktree_mgr.StaleWorktreeError, match="worktree reaper"):
        await worktree_mgr.create_worktree(TASK, repo, base)

    assert (wt / "work.txt").read_text() == "uncommitted task work"
    assert list_entries() == []  # nothing trashed, nothing deleted


@pytest.mark.asyncio
async def test_a_locked_worktree_says_nothing_will_reap_it(repo):
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(wt)], check=True)

    with pytest.raises(worktree_mgr.StaleWorktreeError, match="is locked; nothing reaps"):
        await worktree_mgr.create_worktree(TASK, repo, base)
    assert wt.is_dir() and list_entries() == []


@pytest.mark.asyncio
async def test_an_orphan_directory_goes_to_the_trash_and_creation_proceeds(repo):
    base = repo / ".claude" / "worktrees"
    orphan = base / f"task-{TASK[:8]}"
    orphan.mkdir(parents=True)
    (orphan / "leftover.txt").write_text("from a crashed run")

    wt = await worktree_mgr.create_worktree(TASK, repo, base)

    assert wt == orphan and await worktree_mgr.verify_worktree(wt)
    [entry] = list_entries()
    assert entry.tombstone.caller == "worktree_mgr.create_worktree"
    assert (entry.path / ITEM / "leftover.txt").read_text() == "from a crashed run"


@pytest.mark.asyncio
async def test_an_unreadable_worktree_list_counts_as_registered(repo, monkeypatch):
    # The direction that deletes nothing.
    orphan = repo / ".claude" / "worktrees" / "task-x"
    orphan.mkdir(parents=True)
    real = worktree_mgr.asyncio.create_subprocess_exec

    async def failing_list(*args, **kw):
        if args[:3] == ("git", "worktree", "list"):
            return await real("false", **kw)
        return await real(*args, **kw)

    monkeypatch.setattr(worktree_mgr.asyncio, "create_subprocess_exec", failing_list)
    assert await worktree_mgr._is_registered_worktree(orphan, repo) is True
