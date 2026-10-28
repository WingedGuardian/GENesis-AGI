"""Completion preserves checkout-only history and persisted recovery state."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor import engine as engine_mod
from genesis.autonomy.executor import worktree_mgr
from genesis.autonomy.executor.engine import CCSessionExecutor
from genesis.db.crud import task_states
from genesis.db.crud.task_states import create_intake_token
from tests.test_autonomy.test_executor.test_engine import db as db
from tests.test_autonomy.test_executor.test_worktree_mgr_stale import repo as repo

TASK = "completion-fixture"
BRANCH = f"task/{TASK[:8]}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["attached", "detached", "private"])
async def test_cleanup_preserves_history_with_visible_ref_gc(repo, history):
    wt = await worktree_mgr.create_worktree(TASK, repo, repo / "tasks")
    (wt / ".gitignore").write_text("TASK_NOTEPAD.md\n")
    _git(wt, "add", ".gitignore")
    _git(wt, "commit", "-qm", "ignore private notes")
    if history != "attached":
        _git(wt, "checkout", "-q", "--detach")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "unique task history")
    unique = _git(wt, "rev-parse", "HEAD")
    if history != "attached":
        assert unique not in _git(repo, "rev-list", "--branches").splitlines()
    if history == "private":
        _git(wt, "update-ref", "refs/worktree/saved", unique)
        _git(wt, "checkout", "-q", BRANCH)
        assert _git(wt, "rev-parse", "HEAD") != unique
    (wt / "TASK_NOTEPAD.md").write_bytes(b"private interrupted notes\n")
    assert not _git(wt, "status", "--porcelain")
    registration = _git(repo, "worktree", "list", "--porcelain", "-z")
    refs = _git(repo, "show-ref")

    await worktree_mgr.cleanup_worktree(wt, repo)
    await worktree_mgr.cleanup_worktree(wt, repo)

    assert wt.is_dir()
    assert _git(repo, "worktree", "list", "--porcelain", "-z") == registration
    assert _git(repo, "show-ref") == refs
    assert (wt / "TASK_NOTEPAD.md").read_bytes() == b"private interrupted notes\n"
    # Main-checkout GC does not preserve linked-worktree private refs; use
    # their owning worktree for that case, not a universal-GC durability claim.
    gc_root = wt if history == "private" else repo
    _git(gc_root, "reflog", "expire", "--expire=now", "--all")
    _git(gc_root, "gc", "--prune=now")
    _git(repo, "cat-file", "-e", f"{unique}^{{commit}}")
    if history == "private":
        assert _git(wt, "rev-parse", "refs/worktree/saved") == unique
    else:
        assert _git(wt, "rev-parse", "HEAD") == unique


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["dirty", "locked", "missing"])
async def test_cleanup_never_invokes_git(repo, monkeypatch, state):
    wt = await worktree_mgr.create_worktree(TASK, repo, repo / "tasks")
    if state == "missing":
        shutil.rmtree(wt)
    elif state == "locked":
        _git(repo, "worktree", "lock", str(wt))
    else:
        (wt / "unfinished.txt").write_bytes(b"unfinished work\n")
    registration = _git(repo, "worktree", "list", "--porcelain", "-z")
    tip = _git(repo, "rev-parse", BRANCH)

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"cleanup invoked Git: {args}")

    monkeypatch.setattr(worktree_mgr.asyncio, "create_subprocess_exec", forbidden)
    await worktree_mgr.cleanup_worktree(wt, repo)
    assert _git(repo, "worktree", "list", "--porcelain", "-z") == registration
    assert _git(repo, "rev-parse", BRANCH) == tip
    if state == "dirty":
        assert (wt / "unfinished.txt").read_bytes() == b"unfinished work\n"


@pytest.fixture
def executor(db, monkeypatch):
    from genesis.runtime import GenesisRuntime

    monkeypatch.setattr(GenesisRuntime, "peek", lambda: None)
    return CCSessionExecutor(
        db=db, invoker=AsyncMock(), decomposer=AsyncMock(), reviewer=AsyncMock(),
    )


@pytest.mark.parametrize("entry", ["absent", "file", "empty", "symlink", "directory"])
def test_notepad_seeding_preserves_existing_entries(executor, tmp_path, entry):
    executor._worktree_paths[TASK] = tmp_path
    note = tmp_path / "TASK_NOTEPAD.md"
    target = tmp_path / "target"
    target.write_bytes(b"private target\n")
    if entry == "directory":
        note.mkdir()
    elif entry == "symlink":
        note.symlink_to(target)
    elif entry != "absent":
        note.write_bytes(b"" if entry == "empty" else b"existing notes\n")
    before = note.read_bytes() if entry not in {"absent", "directory"} else None
    executor._seed_notepad(TASK)
    if entry == "absent":
        assert note.read_text() == executor._NOTEPAD_SKELETON
    elif entry == "directory":
        assert note.is_dir()
    else:
        assert note.read_bytes() == before
    assert target.read_bytes() == b"private target\n"
    assert note.is_symlink() == (entry == "symlink")


def test_notepad_creation_race_preserves_winner(executor, tmp_path, monkeypatch):
    executor._worktree_paths[TASK] = tmp_path
    note = tmp_path / "TASK_NOTEPAD.md"
    original = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == note and mode == "x":
            with original(path, "w") as stream:
                stream.write("another creator won")
            raise FileExistsError(str(path))
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    executor._seed_notepad(TASK)
    assert note.read_text() == "another creator won"


@pytest.mark.asyncio
async def test_cleanup_restart_uses_preserved_database_locator(executor, repo, db, monkeypatch):
    monkeypatch.setattr(engine_mod, "_REPO_ROOT", repo)
    monkeypatch.setattr(engine_mod, "_WORKTREE_BASE", repo / "tasks")
    original = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", original)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    token = await create_intake_token(db)
    await task_states.create(
        db, task_id=TASK, description="completion fixture", current_phase="executing",
        intake_token=token,
    )
    wt = await executor._create_worktree(TASK)
    (wt / "TASK_NOTEPAD.md").write_bytes(b"notes survive restart\n")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "task work")
    tip = _git(wt, "rev-parse", "HEAD")
    outputs = json.loads((await task_states.get_by_id(db, TASK))["outputs"])
    _git(repo, "commit", "-q", "--allow-empty", "-m", "default advanced")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    await executor._cleanup_worktree(TASK)
    assert TASK not in executor._worktree_paths and TASK not in executor._bases
    row = await task_states.get_by_id(db, TASK)
    assert json.loads(row["outputs"]) == outputs
    assert outputs["worktree_path"] == str(wt)
    restarted = CCSessionExecutor(
        db=db, invoker=AsyncMock(), decomposer=AsyncMock(), reviewer=AsyncMock(),
    )
    assert await restarted._recover_worktree(TASK, row)
    assert restarted._worktree_paths[TASK] == wt
    assert await restarted._base_for(TASK) == worktree_mgr.BaseRef("main", original)
    assert _git(wt, "rev-parse", "HEAD") == tip
    assert (wt / "TASK_NOTEPAD.md").read_bytes() == b"notes survive restart\n"
