"""Read-only identity checks; executor activation belongs to the next PR."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor import worktree_mgr as mgr
from tests.test_autonomy.test_executor.test_worktree_mgr_stale import repo as repo


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _snapshot(repo: Path, wt: Path) -> tuple:
    files = {}
    for root in (repo / ".git" / "worktrees", wt):
        if root.is_symlink():
            files[str(root)] = ("symlink", str(root.readlink()))
            continue
        for p in root.rglob("*"):
            if p.is_symlink():
                files[str(p)] = ("symlink", str(p.readlink()))
            elif p.is_file():
                files[str(p)] = p.read_bytes()
    return _git(repo, "show-ref"), _git(repo, "worktree", "list", "--porcelain"), files


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    "clean", "dirty", "locked", "prunable", "detached", "wrong_branch", "duplicate",
    "foreign", "symlink", "copied_git", "wrong_tip", "missing_record", "changed_head",
])
async def test_validation_preserves_git_state(repo, state):
    branch, wt = mgr.task_locator("validation-task", repo / "worktrees")
    assert branch == "task/validati" and wt.name == "task-validati"
    _git(repo, "worktree", "add", "-b", branch, str(wt))
    tip = _git(wt, "rev-parse", "HEAD")
    if state == "dirty":
        (wt / "notes.txt").write_bytes(b"staged notes\n")
        _git(wt, "add", "notes.txt")
        (wt / "notes.txt").write_bytes(b"keep local task notes\n")
        (repo / ".git" / "info" / "exclude").write_text("ignored.txt\n")
        (wt / "ignored.txt").write_bytes(b"ignored task data\n")
    elif state == "locked":
        _git(repo, "worktree", "lock", str(wt))
    elif state == "detached":
        _git(wt, "checkout", "--detach")
        _git(wt, "commit", "--allow-empty", "-m", "later detached work")
    elif state == "wrong_branch":
        _git(wt, "checkout", "-b", "other")
    elif state in {"prunable", "foreign", "symlink"}:
        shutil.rmtree(wt)
        if state == "foreign":
            _git(repo, "init", str(wt))
        elif state == "symlink":
            wt.symlink_to(repo)
    elif state == "copied_git":
        sibling = wt.parent / "sibling"
        _git(repo, "worktree", "add", "-b", "other", str(sibling))
        (wt / ".git").write_bytes((sibling / ".git").read_bytes())
    records = await mgr._worktree_records(repo)
    assert records is not None
    target_records = mgr._listed_records(records, wt)
    assert len(target_records) == 1
    if state == "duplicate":
        records += target_records
    elif state == "missing_record":
        records = []
    elif state == "changed_head":
        _git(wt, "checkout", "-b", "other")
    elif state == "wrong_tip":
        _git(repo, "commit", "--allow-empty", "-m", "different tip")
        tip = _git(repo, "rev-parse", "HEAD")
    before = _snapshot(repo, wt)
    if state == "symlink":
        assert str(wt) in before[2]
    if state in {"clean", "dirty"}:
        await mgr._validate_live_worktree(wt, repo, branch, tip, records)
        if state == "dirty":
            assert (wt / "notes.txt").read_bytes() == b"keep local task notes\n"
    else:
        with pytest.raises(mgr.StaleWorktreeError):
            await mgr._validate_live_worktree(wt, repo, branch, tip, records)
    assert _snapshot(repo, wt) == before


@pytest.mark.asyncio
async def test_exact_local_branch_lookup_ignores_same_named_tag(repo):
    branch, _ = mgr.task_locator("validation-task", repo / "worktrees")
    initial = _git(repo, "rev-parse", "HEAD")
    assert await mgr.branch_tip(branch, repo) is None
    assert not await mgr._tag_conflict(branch, repo)
    _git(repo, "tag", branch)
    assert await mgr.branch_tip(branch, repo) is None
    assert await mgr._tag_conflict(branch, repo)
    _git(repo, "commit", "--allow-empty", "-m", "new branch tip")
    tip = _git(repo, "rev-parse", "HEAD")
    assert tip != initial
    _git(repo, "branch", branch)
    before = _git(repo, "show-ref")
    assert await mgr.branch_tip(branch, repo) == tip
    assert _git(repo, "show-ref") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["branch_tip", "_tag_conflict"])
async def test_unreadable_ref_is_not_absence(repo, monkeypatch, helper):
    monkeypatch.setattr(mgr, "_git_read", AsyncMock(return_value=(-1, "")))
    with pytest.raises(mgr.StaleWorktreeError, match="could not be read"):
        await getattr(mgr, helper)("task/unreadable", repo)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [(0, ""), (128, "")])
async def test_unresolvable_commit_is_not_absence(repo, monkeypatch, result):
    monkeypatch.setattr(mgr, "_git_read", AsyncMock(side_effect=[(0, ""), result]))
    with pytest.raises(mgr.StaleWorktreeError, match="does not resolve"):
        await mgr.branch_tip("task/unreadable", repo)


def test_stale_message_identifies_state_for_manual_inspection(tmp_path):
    branch, wt = mgr.task_locator("validation-task", tmp_path)
    error = mgr._task_stale("validation-task", branch, wt, "ref changed")
    assert isinstance(error, mgr.StaleWorktreeError)
    assert all(part in str(error) for part in (
        "validation-task", branch, str(wt), "ref changed", "inspect", "manually",
    ))
