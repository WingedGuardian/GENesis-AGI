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
    "clean", "dirty", "staged", "ignored", "locked", "prunable", "detached",
    "wrong_branch", "duplicate", "foreign", "symlink", "copied_git", "wrong_tip",
    "missing_record", "changed_head",
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
    elif state == "staged":
        (wt / "staged.txt").write_bytes(b"staged task data\n")
        _git(wt, "add", "staged.txt")
    elif state == "ignored":
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
    if state in {"clean", "dirty", "staged", "ignored"}:
        with pytest.raises(mgr.StaleWorktreeError, match="retained task branch"):
            await mgr.create_worktree("validation-task", repo, repo / "worktrees")
        assert _snapshot(repo, wt) == before
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
    _, target = mgr.task_locator("validation-task", repo / "worktrees")
    before = _snapshot(repo, target)
    with pytest.raises(mgr.StaleWorktreeError, match="same-named tag"):
        await mgr.assert_fresh_task("validation-task", repo, target.parent)
    assert _snapshot(repo, target) == before
    _git(repo, "commit", "--allow-empty", "-m", "new branch tip")
    tip = _git(repo, "rev-parse", "HEAD")
    assert tip != initial
    _git(repo, "branch", branch)
    before = _git(repo, "show-ref")
    assert await mgr.branch_tip(branch, repo) == tip
    assert _git(repo, "show-ref") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", ["", "abc/defg", "abc\\defg", "bad..ref", "bad\0id"])
async def test_fresh_guard_refuses_invalid_locators(repo, monkeypatch, task_id):
    read_git = AsyncMock()
    if "\0" in task_id:
        monkeypatch.setattr(mgr, "_git_read", read_git)
    with pytest.raises(mgr.StaleWorktreeError, match="task ID"):
        await mgr.assert_fresh_task(task_id, repo, repo / "worktrees")
    if "\0" in task_id:
        read_git.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("check-ref-format", "branch name could not be checked"),
        ("worktree-list", "git worktree list could not be read"),
    ],
)
async def test_fresh_guard_contextualizes_git_start_failures(repo, monkeypatch, failure, message):
    async def failing_read(root, *args):
        if failure == "check-ref-format" and args[0] == "check-ref-format":
            raise OSError("git could not start")
        if args[0] == "check-ref-format":
            return 0, ""
        if args[:2] == ("show-ref", "--exists"):
            return 2, ""
        if args[:2] == ("worktree", "list"):
            raise OSError("git could not start")
        raise AssertionError(args)

    monkeypatch.setattr(mgr, "_git_read", failing_read)
    with pytest.raises(mgr.StaleWorktreeError, match=message):
        await mgr.assert_fresh_task("validation-task", repo, repo / "worktrees")


@pytest.mark.asyncio
@pytest.mark.parametrize("registration", ["retained_branch", "at_target", "elsewhere"])
async def test_fresh_creation_refuses_branch_and_registration_conflicts(repo, registration):
    branch, target = mgr.task_locator("validation-task", repo / "worktrees")
    if registration == "retained_branch":
        _git(repo, "branch", branch, "HEAD")
        message = "retained task branch"
    elif registration == "at_target":
        _git(repo, "worktree", "add", "-b", "other", str(target))
        message = "target path already has a worktree registration"
    else:
        sibling = repo / "sibling"
        _git(repo, "worktree", "add", "--detach", str(sibling), "HEAD")
        admin = Path(_git(sibling, "rev-parse", "--absolute-git-dir"))
        (admin / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        message = "task branch is registered at another worktree path"
    before = _snapshot(repo, target)

    with pytest.raises(mgr.StaleWorktreeError, match=message):
        await mgr.create_worktree("validation-task", repo, repo / "worktrees")

    assert _snapshot(repo, target) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("race", [
    "pre_add_path", "pre_add_registration", "post_branch", "post_tag", "post_head",
    "post_admin", "pre_claim_empty", "pre_claim_symlink",
])
async def test_creation_refuses_races_without_rollback(repo, monkeypatch, race):
    branch, target = mgr.task_locator("validation-task", repo / "worktrees")
    real_run = mgr.asyncio.create_subprocess_exec
    real_read = mgr._git_read
    real_fresh = mgr.assert_fresh_task
    adds = 0
    freshness_checks = 0
    snapshots = []
    reads = {}
    foreign = repo / "foreign"
    if race == "pre_claim_symlink":
        foreign.mkdir()
    foreign_before = _snapshot(repo, foreign) if race == "pre_claim_symlink" else None

    async def intercept(*args, **kwargs):
        nonlocal adds
        if args[:3] == ("git", "worktree", "add"):
            adds += 1
            if race == "pre_add_path":
                target.mkdir(parents=True, exist_ok=True)
                (target / "leftover").write_bytes(b"keep")
            elif race == "pre_add_registration":
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "add", "-b", "other", str(target)],
                    check=True,
                )
            if race.startswith("pre_add"):
                snapshots.append(_snapshot(repo, target))
        return await real_run(*args, **kwargs)

    async def fresh(*args):
        nonlocal freshness_checks
        result = await real_fresh(*args)
        freshness_checks += 1
        if freshness_checks == 2 and race.startswith("pre_claim"):
            target.parent.mkdir(parents=True, exist_ok=True)
            if race == "pre_claim_empty":
                target.mkdir()
            else:
                target.symlink_to(foreign, target_is_directory=True)
            snapshots.append(_snapshot(repo, target))
        return result

    async def read(root, *args):
        if args[:2] == ("show-ref", "--exists"):
            reads[args[2]] = reads.get(args[2], 0) + 1
            if race == "post_branch" and args[2] == f"refs/heads/{branch}" and reads[args[2]] == 3:
                subprocess.run(
                    ["git", "-C", str(target), "commit", "--allow-empty", "-m", "substitute"],
                    check=True,
                )
                snapshots.append(_snapshot(repo, target))
            if race == "post_tag" and args[2] == f"refs/tags/{branch}" and reads[args[2]] == 3:
                subprocess.run(["git", "-C", str(repo), "tag", branch], check=True)
                snapshots.append(_snapshot(repo, target))
        if race == "post_head" and args[:3] == ("symbolic-ref", "--quiet", "HEAD"):
            subprocess.run(
                ["git", "-C", str(root), "symbolic-ref", "HEAD", "refs/heads/main"],
                check=True,
            )
            snapshots.append(_snapshot(repo, target))
        if (
            race == "post_admin"
            and Path(root) == target
            and args[:2] == ("rev-parse", "--absolute-git-dir")
        ):
            admin = Path(_git(target, "rev-parse", "--absolute-git-dir"))
            (admin / "gitdir").write_text("/wrong/.git\n")
            snapshots.append(_snapshot(repo, target))
        return await real_read(root, *args)

    monkeypatch.setattr(mgr.asyncio, "create_subprocess_exec", intercept)
    monkeypatch.setattr(mgr, "_git_read", read)
    monkeypatch.setattr(mgr, "assert_fresh_task", fresh)

    with pytest.raises(mgr.StaleWorktreeError):
        await mgr.create_worktree("validation-task", repo, repo / "worktrees")

    after = _snapshot(repo, target)
    if race.startswith("pre_claim"):
        if race == "pre_claim_symlink":
            assert target.is_symlink() and not any(foreign.iterdir())
            assert _snapshot(repo, foreign) == foreign_before
        assert after == snapshots[0]
    elif race.startswith("pre_add"):
        after, snapshots[0] = after[1:], snapshots[0][1:]
        assert after == snapshots[0]
    else:
        assert after == snapshots[0]
    assert adds == (0 if race.startswith("pre_claim") else 1)


@pytest.mark.asyncio
async def test_late_dangling_task_ref_preserves_git_created_checkout(repo, monkeypatch):
    branch, target = mgr.task_locator("validation-task", repo / "worktrees")
    head = _git(repo, "rev-parse", "HEAD")
    real_run = mgr.asyncio.create_subprocess_exec
    injected = False

    async def intercept(*args, **kwargs):
        nonlocal injected
        if args[:3] == ("git", "worktree", "add") and not injected:
            ref = repo / ".git" / "refs" / "heads" / branch
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_text(f"ref: refs/heads/{branch}-referent\n")
            injected = True
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(mgr.asyncio, "create_subprocess_exec", intercept)
    with pytest.raises(mgr.StaleWorktreeError):
        await mgr.create_worktree("validation-task", repo, repo / "worktrees")

    assert injected and target.is_dir() and (target / ".git").is_file()
    assert await mgr.branch_tip(f"{branch}-referent", repo) == head
    assert _git(repo, "rev-parse", "main") == head
    assert str(target) in _git(repo, "worktree", "list", "--porcelain")


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["detached", "private"])
async def test_fresh_creation_preserves_unrelated_sibling_history(repo, history):
    sibling = repo / "sibling"
    _git(repo, "worktree", "add", "--detach", str(sibling), "HEAD")
    _git(sibling, "commit", "--allow-empty", "-m", "detached-only")
    assert not _git(repo, "branch", "--contains", _git(sibling, "rev-parse", "HEAD"))
    private_tip = None
    if history == "private":
        tree = _git(repo, "rev-parse", "HEAD^{tree}")
        private_tip = _git(repo, "commit-tree", tree, "-m", "private-only")
        assert private_tip != _git(repo, "rev-parse", "HEAD")
        assert not _git(repo, "branch", "--contains", private_tip)
        _git(sibling, "update-ref", "refs/worktree/saved", private_tip)
    admin = Path(_git(sibling, "rev-parse", "--absolute-git-dir"))
    ref = admin / "refs" / "worktree" / "saved"
    ref_bytes = ref.read_bytes() if history == "private" else None
    shutil.rmtree(sibling)
    before = _snapshot(repo, sibling)[2]

    await mgr.create_worktree("fresh-task", repo, repo / "worktrees")

    after = _snapshot(repo, sibling)[2]
    assert all(after[path] == value for path, value in before.items())
    assert str(sibling) in _git(repo, "worktree", "list", "--porcelain")
    if history == "private":
        assert ref.read_bytes() == ref_bytes
        assert _git(repo, "rev-parse", f"worktrees/{admin.name}/refs/worktree/saved") == private_tip


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


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["not a sha\n", ""], ids=["garbage", "empty"])
@pytest.mark.parametrize(
    ("helper", "folder"), [("branch_tip", "heads"), ("_tag_conflict", "tags")],
)
async def test_a_corrupt_ref_is_not_absence_on_real_git(repo, helper, folder, content):
    """git's own exit codes: a mock of them proved nothing about these states."""
    (repo / ".git" / "refs" / folder / "corrupt").write_text(content)
    with pytest.raises(mgr.StaleWorktreeError, match="could not be read"):
        await getattr(mgr, helper)("corrupt", repo)


@pytest.mark.asyncio
async def test_a_dangling_branch_ref_is_not_absence_on_real_git(repo):
    (repo / ".git" / "refs" / "heads" / "dangling").write_text("ref: refs/heads/nowhere\n")
    with pytest.raises(mgr.StaleWorktreeError):
        await mgr.branch_tip("dangling", repo)


@pytest.mark.asyncio
async def test_a_truly_missing_ref_is_absence_on_real_git(repo):
    assert await mgr.branch_tip("nothing-here", repo) is None
    assert await mgr._tag_conflict("nothing-here", repo) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["clone", "symlink", "copied_git"])
async def test_a_stand_in_on_the_same_branch_and_tip_is_refused(repo, state):
    """Only the reciprocal registration check can catch these: the branch and the
    tip both match, so the HEAD comparison passes."""
    branch, wt = mgr.task_locator("validation-task", repo / "worktrees")
    _git(repo, "worktree", "add", "-b", branch, str(wt))
    tip = _git(wt, "rev-parse", "HEAD")
    sibling = wt.parent / "sibling"
    if state == "clone":
        shutil.rmtree(wt)
        _git(repo, "clone", "-q", "-b", branch, str(repo), str(wt))
    else:
        _git(repo, "worktree", "add", "--force", str(sibling), branch)
        if state == "symlink":
            shutil.rmtree(wt)
            wt.symlink_to(sibling)
        else:
            (wt / ".git").write_bytes((sibling / ".git").read_bytes())
    records = await mgr._worktree_records(repo)
    assert records is not None
    with pytest.raises(mgr.StaleWorktreeError, match="not safely registered"):
        await mgr._validate_live_worktree(wt, repo, branch, tip, records)


def test_stale_message_identifies_state_for_manual_inspection(tmp_path):
    branch, wt = mgr.task_locator("validation-task", tmp_path)
    error = mgr._task_stale("validation-task", branch, wt, "ref changed")
    assert isinstance(error, mgr.StaleWorktreeError)
    assert all(part in str(error) for part in (
        "validation-task", branch, str(wt), "ref changed", "inspect", "manually",
    ))
