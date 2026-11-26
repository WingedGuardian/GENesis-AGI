"""Task worktree creation refuses every retained or occupied locator.

Real git, scratch repositories. Before this change a stale task worktree that
``git worktree remove`` refused (uncommitted changes) was ``rmtree``d, and the
re-create then failed anyway because the branch and registration survived
(MEASURED). Creation now refuses retained branches and occupied paths without
changing either one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from genesis.autonomy.executor import worktree_mgr


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

    with pytest.raises(worktree_mgr.StaleWorktreeError, match="retained task branch"):
        await worktree_mgr.create_worktree(TASK, repo, base)

    assert (wt / "work.txt").read_text() == "uncommitted task work"


@pytest.mark.asyncio
async def test_a_newline_in_the_repo_path_does_not_hide_a_registered_worktree(tmp_path, repo):
    # git 2.43 prints a newline in a path literally in line mode; -z keeps it whole.
    odd = tmp_path / "re\npo"
    subprocess.run(["git", "clone", "-q", str(repo), str(odd)], check=True)
    base = odd / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, odd, base)
    (wt / "work.txt").write_text("uncommitted")
    subprocess.run(["git", "-C", str(wt), "add", "work.txt"], check=True)

    records = await worktree_mgr._worktree_records(odd)
    assert worktree_mgr._record_for(records, wt) is not None
    with pytest.raises(worktree_mgr.StaleWorktreeError, match="retained task branch"):
        await worktree_mgr.create_worktree(TASK, odd, base)
    assert (wt / "work.txt").read_text() == "uncommitted"


@pytest.mark.asyncio
async def test_a_locked_worktree_says_nothing_will_reap_it(repo):
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(wt)], check=True)

    with pytest.raises(worktree_mgr.StaleWorktreeError, match="retained task branch"):
        await worktree_mgr.create_worktree(TASK, repo, base)
    assert wt.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["directory", "file", "dangling_symlink"])
async def test_an_occupied_task_path_is_refused_without_changing_it(repo, kind):
    base = repo / ".claude" / "worktrees"
    orphan = base / f"task-{TASK[:8]}"
    orphan.parent.mkdir(parents=True)
    if kind == "directory":
        orphan.mkdir()
        (orphan / "leftover.txt").write_text("from a crashed run")
    elif kind == "file":
        orphan.write_text("not a worktree")
    else:
        orphan.symlink_to(orphan.parent / "missing")

    with pytest.raises(worktree_mgr.StaleWorktreeError, match="target path is already occupied"):
        await worktree_mgr.create_worktree(TASK, repo, base)

    assert orphan.exists() is (kind != "dangling_symlink")
    assert orphan.is_symlink() is (kind == "dangling_symlink")
    if kind == "directory":
        assert (orphan / "leftover.txt").read_text() == "from a crashed run"
    elif kind == "file":
        assert orphan.read_text() == "not a worktree"


@pytest.mark.asyncio
async def test_an_unreadable_worktree_list_deletes_nothing(repo, monkeypatch):
    base = repo / ".claude" / "worktrees"
    orphan = base / f"task-{TASK[:8]}"
    orphan.mkdir(parents=True)
    (orphan / "leftover.txt").write_text("keep")
    real = worktree_mgr.asyncio.create_subprocess_exec

    async def failing_list(*args, **kw):
        if args[:3] == ("git", "worktree", "list"):
            return await real("false", **kw)
        return await real(*args, **kw)

    monkeypatch.setattr(worktree_mgr.asyncio, "create_subprocess_exec", failing_list)
    with pytest.raises(worktree_mgr.StaleWorktreeError, match="could not be read"):
        await worktree_mgr.create_worktree(TASK, repo, base)
    assert (orphan / "leftover.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_an_empty_worktree_listing_reads_as_unreadable(monkeypatch, tmp_path):
    # git always lists the main worktree, so nothing listed means nothing read.
    async def empty(*a, **k):
        return 0, ""

    monkeypatch.setattr(worktree_mgr, "_git_read", empty)
    assert await worktree_mgr._worktree_records(tmp_path) is None


@pytest.mark.asyncio
async def test_an_orphan_directory_is_not_a_registered_worktree(repo):
    # #3021: git rev-parse inside an orphan under the repo finds the main repo,
    # so a resumed task used to run against the main checkout.
    orphan = repo / ".claude" / "worktrees" / "task-orphan"
    orphan.mkdir(parents=True)
    rc = subprocess.run(["git", "-C", str(orphan), "rev-parse", "--git-dir"], capture_output=True)
    assert rc.returncode == 0  # the trap: rev-parse says yes
    assert await worktree_mgr.is_registered_worktree(orphan, repo) is False
    wt = await worktree_mgr.create_worktree(TASK, repo, repo / ".claude" / "worktrees")
    assert await worktree_mgr.is_registered_worktree(wt, repo) is True


@pytest.mark.asyncio
async def test_ambient_git_location_variables_are_ignored_throughout(tmp_path, repo, monkeypatch):
    # Set before the worktree exists: every manager Git read must still use repo.
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    assert await worktree_mgr.is_registered_worktree(wt, repo) is True
    (wt / "work.txt").write_text("uncommitted")
    subprocess.run(
        ["git", "-C", str(wt), "add", "work.txt"],
        check=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    with pytest.raises(worktree_mgr.StaleWorktreeError, match="retained task branch"):
        await worktree_mgr.create_worktree(TASK, repo, base)
    assert (wt / "work.txt").read_text() == "uncommitted"


@pytest.mark.asyncio
async def test_an_unreadable_listing_never_recreates_a_live_worktree(repo, monkeypatch):
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    (wt / "step.txt").write_text("step one")
    subprocess.run(["git", "-C", str(wt), "add", "step.txt"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "step"], check=True)

    async def unreadable(*a, **k):
        return -1, ""

    monkeypatch.setattr(worktree_mgr, "_git_read", unreadable)
    with pytest.raises(worktree_mgr.StaleWorktreeError, match="could not be read"):
        await worktree_mgr.is_registered_worktree(wt, repo)
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s", f"task/{TASK[:8]}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "step" in log and (wt / "step.txt").exists()


def _delete_and_recreate(wt: Path) -> None:
    """The directory goes outside git and an orphan takes its place."""
    shutil.rmtree(wt)
    wt.mkdir()
    (wt / "leftover.txt").write_text("orphan")


def _porcelain(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.asyncio
async def test_a_prunable_record_does_not_make_an_orphan_registered(repo):
    # git keeps the record, marked "prunable gitdir file points to non-existent
    # location"; a resumed task must not adopt the directory (#3061 review).
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    _delete_and_recreate(wt)
    assert "prunable" in _porcelain(repo)  # the trap: git still lists it
    assert await worktree_mgr.is_registered_worktree(wt, repo) is False


@pytest.mark.asyncio
async def test_a_prunable_orphan_and_retained_branch_are_left_unchanged(repo):
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    _delete_and_recreate(wt)
    before = _porcelain(repo)
    tip = await worktree_mgr.branch_tip(f"task/{TASK[:8]}", repo)

    with pytest.raises(worktree_mgr.StaleWorktreeError, match="retained task branch"):
        await worktree_mgr.create_worktree(TASK, repo, base)

    assert (wt / "leftover.txt").read_text() == "orphan"
    assert _porcelain(repo) == before
    assert await worktree_mgr.branch_tip(f"task/{TASK[:8]}", repo) == tip


@pytest.mark.asyncio
async def test_a_locked_record_does_not_make_an_orphan_registered(repo):
    # A locked record is never marked prunable, so the listing alone would
    # still call the re-created directory a worktree.
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(wt)], check=True)
    _delete_and_recreate(wt)
    assert "prunable" not in _porcelain(repo)
    assert await worktree_mgr.is_registered_worktree(wt, repo) is False
    # A retained branch refuses before the occupied locked path is changed.
    with pytest.raises(worktree_mgr.StaleWorktreeError, match="retained task branch"):
        await worktree_mgr.create_worktree(TASK, repo, base)
    assert (wt / "leftover.txt").read_text() == "orphan"


@pytest.mark.asyncio
async def test_command_line_config_reaches_every_git_call(repo, monkeypatch):
    # safe.directory is accepted only from protected config, which includes
    # GIT_CONFIG_COUNT and GIT_CONFIG_PARAMETERS; scrubbing them breaks an
    # install whose checkout uid differs from the executor's (#3061 review).
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'genesis.probe'='yes'")
    assert await worktree_mgr._git_read(repo, "config", "--get", "safe.directory") == (0, "*")
    assert await worktree_mgr._git_read(repo, "config", "--get", "genesis.probe") == (0, "yes")


@pytest.mark.asyncio
async def test_a_reinitialised_directory_is_not_the_registered_worktree(repo):
    # git init at the old path: its own top level, and the old record is not
    # marked prunable because a .git exists there again.
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    shutil.rmtree(wt)
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    assert "prunable" not in _porcelain(repo)
    assert await worktree_mgr.is_registered_worktree(wt, repo) is False


@pytest.mark.asyncio
async def test_a_task_path_replaced_by_a_link_to_the_checkout_is_not_registered(repo):
    # Resolving both sides matched the main checkout's own record and top
    # level, so a resumed task would run in the checkout (#3061 review).
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    shutil.rmtree(wt)
    wt.symlink_to(repo)
    assert await worktree_mgr.is_registered_worktree(wt, repo) is False


@pytest.mark.asyncio
async def test_a_newline_in_the_repo_path_keeps_a_live_worktree_registered(tmp_path, repo):
    odd = tmp_path / "re\npo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(odd)], check=True)
    subprocess.run(
        ["git", "-C", str(odd), "commit", "-q", "--allow-empty", "-m", "init"], check=True
    )
    wt = await worktree_mgr.create_worktree(TASK, odd, odd / ".claude" / "worktrees")
    assert await worktree_mgr.is_registered_worktree(wt, odd) is True


OTHER = "0123456789abcdef"


@pytest.mark.asyncio
async def test_a_dot_git_copied_from_another_task_is_not_registered(repo):
    # The copy names task A's admin dir, whose gitdir points back at A, not
    # here; adopting it would run task B on task A's branch and index.
    base = repo / ".claude" / "worktrees"
    a = await worktree_mgr.create_worktree(OTHER, repo, base)
    b = await worktree_mgr.create_worktree(TASK, repo, base)
    (b / ".git").write_text((a / ".git").read_text())
    assert await worktree_mgr.is_registered_worktree(a, repo) is True
    assert await worktree_mgr.is_registered_worktree(b, repo) is False


@pytest.mark.asyncio
async def test_a_task_path_linked_to_another_worktree_is_not_registered(repo):
    base = repo / ".claude" / "worktrees"
    a = await worktree_mgr.create_worktree(OTHER, repo, base)
    b = await worktree_mgr.create_worktree(TASK, repo, base)
    shutil.rmtree(b)
    b.symlink_to(a)
    assert await worktree_mgr.is_registered_worktree(b, repo) is False
    with pytest.raises(worktree_mgr.StaleWorktreeError, match="retained task branch"):
        await worktree_mgr.create_worktree(TASK, repo, base)
    assert a.is_dir() and b.is_symlink()


@pytest.mark.asyncio
async def test_a_live_worktree_under_a_linked_base_is_registered(tmp_path, repo):
    # The positive case through a symlinked parent: git records the real path,
    # the executor names the linked one; both are compared as named.
    real_base = tmp_path / "real-base"
    real_base.mkdir()
    linked = tmp_path / "linked-base"
    linked.symlink_to(real_base)
    wt = await worktree_mgr.create_worktree(TASK, repo, linked)
    assert await worktree_mgr.is_registered_worktree(wt, repo) is True
    assert await worktree_mgr.is_registered_worktree(Path(os.path.relpath(wt)), repo) is True


@pytest.mark.asyncio
async def test_an_unreadable_admin_record_raises_rather_than_recreating(repo, monkeypatch):
    base = repo / ".claude" / "worktrees"
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    real = Path.read_text

    def denied(self, *a, **k):
        if self.name == "gitdir":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(worktree_mgr.StaleWorktreeError, match="could not be read"):
        await worktree_mgr.is_registered_worktree(wt, repo)
