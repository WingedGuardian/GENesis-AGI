"""The engine must consume the manager's retained-history contract."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor import engine as engine_mod
from genesis.autonomy.executor import worktree_mgr
from genesis.autonomy.executor.engine import CCSessionExecutor
from genesis.autonomy.executor.review import VerifyResult
from genesis.autonomy.executor.types import StepResult
from genesis.db.crud import task_states, task_steps
from genesis.db.crud.task_states import create_intake_token
from tests.test_autonomy.test_executor.test_engine import db as db
from tests.test_autonomy.test_executor.test_worktree_mgr_stale import repo as repo

TASK = "abcdef1234567890"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
async def fixture_engine(db, repo, tmp_path, monkeypatch):
    from genesis.runtime import GenesisRuntime

    base = tmp_path / "task-worktrees"
    monkeypatch.setattr(engine_mod, "_REPO_ROOT", repo)
    monkeypatch.setattr(engine_mod, "_WORKTREE_BASE", base)
    monkeypatch.setattr(GenesisRuntime, "peek", lambda: None)
    engine = CCSessionExecutor(
        db=db, invoker=AsyncMock(), decomposer=AsyncMock(), reviewer=AsyncMock(),
    )
    engine._reviewer.verify_deliverable.return_value = VerifyResult(passed=True)
    engine._step_dispatcher.execute_step = AsyncMock(
        return_value=StepResult(idx=1, status="completed", result="synthetic result"),
    )
    monkeypatch.setattr(engine_mod, "_run_git_push", AsyncMock(return_value=(0, "")))
    engine._open_build_pr = AsyncMock()
    plan = tmp_path / "plan.md"
    plan.write_text("# Synthetic plan\nDo the fixture task.\n")
    token = await create_intake_token(db)
    await task_states.create(
        db, task_id=TASK, description="Synthetic retained-history task",
        current_phase="planning", outputs=json.dumps({"plan_path": str(plan)}),
        intake_token=token,
    )
    return engine, base, plan


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["untracked", "tracked-edit", "staged", "ignored", "empty", "symlink", "directory"])
async def test_adopted_notepad_entry_is_never_replaced(fixture_engine, repo, tmp_path, state):
    engine, base, _ = fixture_engine
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    note = wt / "TASK_NOTEPAD.md"
    target = tmp_path / "private-note.md"
    marker = b"User notes from an interrupted run.\n"
    if state == "directory":
        note.mkdir()
    elif state == "symlink":
        target.write_bytes(marker)
        note.symlink_to(target)
    else:
        note.write_bytes(b"committed note" if state == "tracked-edit" else marker)
        if state == "tracked-edit":
            _git(wt, "add", "TASK_NOTEPAD.md")
            _git(wt, "commit", "-qm", "seed note")
            note.write_bytes(marker)
        elif state == "staged":
            _git(wt, "add", "TASK_NOTEPAD.md")
        elif state == "ignored":
            (wt / ".gitignore").write_text("TASK_NOTEPAD.md\n")
        elif state == "empty":
            note.write_bytes(b"")
    expected = None if state == "directory" else note.read_bytes()
    assert await engine._create_worktree(TASK) == wt
    engine._seed_notepad(TASK)
    if state == "directory":
        assert note.is_dir()
    else:
        assert note.read_bytes() == expected
        if state == "symlink":
            assert note.is_symlink() and target.read_bytes() == marker


@pytest.mark.asyncio
async def test_absent_notepad_is_seeded(fixture_engine):
    engine, _, _ = fixture_engine
    wt = await engine._create_worktree(TASK)
    engine._seed_notepad(TASK)
    assert (wt / "TASK_NOTEPAD.md").read_text() == engine._NOTEPAD_SKELETON


@pytest.mark.asyncio
async def test_notepad_creator_winning_the_race_is_preserved(fixture_engine, monkeypatch):
    engine, _, _ = fixture_engine
    wt = await engine._create_worktree(TASK)
    note = wt / "TASK_NOTEPAD.md"
    original = Path.open

    def another_creator(path, mode="r", *args, **kwargs):
        if path == note and mode == "x":
            with original(path, "w", encoding="utf-8") as stream:
                stream.write("another creator won")
            raise FileExistsError(str(path))
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", another_creator)
    engine._seed_notepad(TASK)
    assert note.read_text() == "another creator won"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["executing", "paused", "blocked"])
@pytest.mark.parametrize("path_state", ["missing", "null", "empty", "legacy"])
async def test_resume_never_substitutes_a_fresh_branch(
    fixture_engine, repo, db, phase, path_state,
):
    engine, _, plan = fixture_engine
    output = {"plan_path": str(plan)}
    if path_state != "missing":
        output["worktree_path"] = None if path_state == "null" else ""
    raw = str(plan) if path_state == "legacy" else json.dumps(output)
    await task_states.update(db, TASK, current_phase=phase, outputs=raw)
    await task_steps.create_step(db, task_id=TASK, step_idx=0, status="completed")
    await task_steps.update_step(db, TASK, 0, result_json='{"result":"previous work"}')
    await task_steps.create_step(db, task_id=TASK, step_idx=1)
    await engine.execute(TASK)
    present = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/task/abcdef12"],
    )
    assert present.returncode == 1, "resume created a new task branch for completed work"
    engine._step_dispatcher.execute_step.assert_not_awaited()
    engine_mod._run_git_push.assert_not_awaited()
    engine._open_build_pr.assert_not_awaited()
    row = await task_states.get_by_id(db, TASK)
    assert row["blockers"]
    assert row["current_phase"] == (phase if phase in {"paused", "blocked"} else "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["wrong-branch", "detached", "locked", "other-worktree"])
async def test_live_recovery_uses_task_identity_not_only_repo_ownership(
    fixture_engine, repo, db, identity,
):
    engine, base, plan = fixture_engine
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    tip = _git(wt, "rev-parse", "HEAD")
    (wt / "keep.txt").write_text("uncommitted fixture work")
    supplied = wt
    if identity == "wrong-branch":
        _git(wt, "switch", "-qc", "other")
    elif identity == "detached":
        _git(wt, "checkout", "-q", "--detach")
    elif identity == "locked":
        _git(repo, "worktree", "lock", str(wt))
    else:
        supplied = repo.parent / "foreign-worktree"
        _git(repo, "worktree", "add", "-qb", "other", str(supplied), "main")
        (supplied / "keep.txt").write_text("another task's dirty work")
    task = {"outputs": json.dumps({"plan_path": str(plan), "worktree_path": str(supplied)})}
    with pytest.raises(worktree_mgr.StaleWorktreeError):
        await engine._recover_worktree(TASK, task)
    assert TASK not in engine._worktree_paths
    assert _git(repo, "rev-parse", "refs/heads/task/abcdef12") == tip
    assert (wt / "keep.txt").read_text() == "uncommitted fixture work"
    if supplied != wt:
        assert (supplied / "keep.txt").read_text() == "another task's dirty work"


@pytest.mark.asyncio
async def test_matching_dirty_live_history_is_adopted(fixture_engine, repo):
    engine, base, plan = fixture_engine
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    (wt / "keep.txt").write_text("preserve dirty content")
    assert await engine._recover_worktree(
        TASK, {"outputs": json.dumps({"plan_path": str(plan), "worktree_path": str(wt)})},
    )
    assert engine._worktree_paths[TASK] == wt
    assert (wt / "keep.txt").read_text() == "preserve dirty content"


@pytest.mark.asyncio
async def test_retained_history_keeps_its_base_after_default_advances(fixture_engine, repo, db):
    engine, base, _ = fixture_engine
    original = _git(repo, "rev-parse", "HEAD")
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    (wt / "history.txt").write_text("unpublished committed work")
    _git(wt, "add", "history.txt")
    _git(wt, "commit", "-qm", "task work")
    tip = _git(wt, "rev-parse", "HEAD")
    await engine._set_output(TASK, "base_branch", "main")
    await engine._set_output(TASK, "base_sha", original)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "default advanced")
    advanced = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", advanced)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert original != advanced
    assert await engine._create_worktree(TASK) == wt
    row = await task_states.get_by_id(db, TASK)
    out = json.loads(row["outputs"])
    assert (out["base_branch"], out["base_sha"]) == ("main", original)
    assert _git(wt, "rev-parse", "HEAD") == tip
    assert await engine._base_for(TASK) == worktree_mgr.BaseRef(name="main", sha=original)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "", "not-json", "[]", "null", "{}", '{"worktree_path":null}', '{"worktree_path":""}'])
async def test_absent_path_metadata_still_requires_and_attaches_history(fixture_engine, repo, raw):
    engine, base, _ = fixture_engine
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    tip = _git(wt, "rev-parse", "HEAD")
    _git(repo, "worktree", "remove", str(wt))
    assert not wt.exists()
    assert await engine._recover_worktree(TASK, {"outputs": raw})
    assert engine._worktree_paths[TASK] == wt
    assert _git(wt, "rev-parse", "HEAD") == tip
    assert await engine._base_for(TASK) is None  # Legacy original cut is unknown.


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, False, [], {}, "another-task", "bad\x00path"])
async def test_present_invalid_path_never_accepts_retained_history(fixture_engine, repo, value):
    engine, base, _ = fixture_engine
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    (wt / "keep.txt").write_text("retained dirty bytes")
    with pytest.raises(worktree_mgr.StaleWorktreeError):
        await engine._recover_worktree(TASK, {"outputs": json.dumps({"worktree_path": value})})
    assert TASK not in engine._worktree_paths
    assert (wt / "keep.txt").read_text() == "retained dirty bytes"


@pytest.mark.asyncio
async def test_new_cut_records_current_remote_default(fixture_engine, repo, db):
    engine, _, _ = fixture_engine
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", tip)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    wt = await engine._create_worktree(TASK)
    assert _git(wt, "rev-parse", "HEAD") == tip
    out = json.loads((await task_states.get_by_id(db, TASK))["outputs"])
    assert (out["base_branch"], out["base_sha"]) == ("main", tip)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("output unavailable"), asyncio.CancelledError()])
async def test_output_failure_cleans_only_validated_tree_and_retains_dirty_work(
    fixture_engine, repo, db, failure,
):
    engine, base, _ = fixture_engine
    wt = await worktree_mgr.create_worktree(TASK, repo, base)
    (wt / "keep.txt").write_text("retained after output failure")
    output_writer = engine._set_output
    engine._set_output = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)):
        await engine._recover_worktree(TASK, {"outputs": "{}"})
    assert engine._worktree_paths[TASK] == wt
    engine._set_output = output_writer
    await engine._cleanup_worktree(TASK)
    assert TASK not in engine._worktree_paths
    assert (wt / "keep.txt").read_text() == "retained after output failure"
    out = json.loads((await task_states.get_by_id(db, TASK))["outputs"])
    assert out["worktree_path"] == ""
    assert await engine._recover_worktree(TASK, {"outputs": json.dumps(out)})
    assert engine._worktree_paths[TASK] == wt
