from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.autonomy.executor import engine as engine_module
from genesis.autonomy.executor import worktree_mgr
from genesis.db.crud import task_states
from genesis.db.schema import create_all_tables
from tests.test_autonomy.test_executor.test_engine import (
    _make_engine,
    _seed_task,
    mock_decomposer,  # noqa: F401
    mock_invoker,  # noqa: F401
    mock_reviewer,  # noqa: F401
    plan_file,  # noqa: F401
)
from tests.test_autonomy.test_executor.test_worktree_mgr_stale import repo as repo
from tests.test_autonomy.test_executor.test_worktree_validation import _git


@pytest.fixture
async def db(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "state.sqlite"))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
async def engine(
    db, repo, monkeypatch, plan_file, mock_invoker, mock_decomposer, mock_reviewer,  # noqa: F811
):
    await _seed_task(db, task_id="t-retained-001", plan_path=plan_file)
    base = repo / ".claude" / "worktrees"
    monkeypatch.setattr(engine_module, "_REPO_ROOT", repo)
    monkeypatch.setattr(engine_module, "_WORKTREE_BASE", base)
    return _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relative_paths", "fail_path_output"),
    [(False, False), (True, False), (False, True)],
)
async def test_fresh_execute_creates_and_dispatches_for_absolute_and_relative_admin(
    db, repo, monkeypatch, engine, mock_invoker, relative_paths, fail_path_output,  # noqa: F811
):
    _git(repo, "config", "worktree.useRelativePaths", str(relative_paths).lower())
    if fail_path_output:
        write_output = engine._set_output

        async def fail_path(task_id, key, value):
            if key == "worktree_path":
                assert task_id not in engine._worktree_paths
                raise OSError("output write failed")
            await write_output(task_id, key, value)

        monkeypatch.setattr(engine, "_set_output", fail_path)

    result = await engine.execute("t-retained-001")

    task = await task_states.get_by_id(db, "t-retained-001")
    outputs = json.loads(task["outputs"])
    wt = worktree_mgr.task_locator(
        "t-retained-001", repo / ".claude" / "worktrees",
    )[1]
    assert _git(wt, "rev-parse", "--show-toplevel") == str(wt)
    admin = Path(_git(wt, "rev-parse", "--absolute-git-dir"))
    back = Path((admin / "gitdir").read_text().strip())
    assert back.is_absolute() is not relative_paths
    assert (admin / back).resolve(strict=True) == (wt / ".git").resolve(strict=True)
    assert result is not fail_path_output
    if fail_path_output:
        assert "worktree_path" not in outputs
        mock_invoker.run.assert_not_awaited()
    else:
        assert outputs["worktree_path"] == str(wt)
        assert any(
            call.args and call.args[0].working_dir == str(wt)
            for call in mock_invoker.run.call_args_list
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("appearance", ["before_preflight", "after_preflight"])
async def test_retained_execute_refuses_before_dispatch_or_base_overwrite(
    db, repo, monkeypatch, engine, mock_invoker, appearance,  # noqa: F811
):
    task_id = "t-retained-001"
    branch, wt = worktree_mgr.task_locator(task_id, repo / ".claude" / "worktrees")
    _git(repo, "commit", "--allow-empty", "-m", "unpublished task work")
    if appearance == "before_preflight":
        _git(repo, "branch", branch, "HEAD")
    for key, value in {
        "base_branch": "saved-main",
        "base_sha": "saved-sha",
        "worktree_path": "/saved/task/path",
    }.items():
        await engine._set_output(task_id, key, value)
    if appearance == "before_preflight":
        resolve_base = AsyncMock()
        monkeypatch.setattr(worktree_mgr, "resolve_base", resolve_base)
    else:
        preflight = worktree_mgr.assert_fresh_task
        calls = 0

        async def appear_after(*args):
            nonlocal calls
            calls += 1
            result = await preflight(*args)
            if calls == 1:
                _git(repo, "branch", branch, "HEAD")
            return result

        monkeypatch.setattr(worktree_mgr, "assert_fresh_task", appear_after)
    saved = json.loads((await task_states.get_by_id(db, task_id))["outputs"])

    assert not await engine.execute(task_id)

    mock_invoker.run.assert_not_awaited()
    current = json.loads((await task_states.get_by_id(db, task_id))["outputs"])
    assert current["worktree_path"] == saved["worktree_path"] and not wt.exists()
    if appearance == "before_preflight":
        resolve_base.assert_not_awaited()
        assert {key: current[key] for key in ("base_branch", "base_sha")} == {
            key: saved[key] for key in ("base_branch", "base_sha")
        }
    else:
        assert (current["base_branch"], current["base_sha"], calls) == ("", "", 2)
        assert await worktree_mgr.branch_tip(branch, repo) is not None


@pytest.mark.asyncio
async def test_cancelled_add_leaves_checkout_uncached_and_unpersisted(
    db, repo, monkeypatch, engine, mock_invoker,  # noqa: F811
):
    real_start = worktree_mgr.asyncio.create_subprocess_exec
    started, hold = asyncio.Event(), asyncio.Event()
    drains = []

    async def start(*args, **kwargs):
        process = await real_start(*args, **kwargs)
        if args[:3] == ("git", "worktree", "add"):
            drains.append(asyncio.create_task(process.communicate()))
            started.set()
            await hold.wait()
        return process

    monkeypatch.setattr(worktree_mgr.asyncio, "create_subprocess_exec", start)
    task = asyncio.create_task(engine.execute("t-retained-001"))
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    assert not await task
    await asyncio.gather(*drains)
    wt = worktree_mgr.task_locator("t-retained-001", repo / ".claude" / "worktrees")[1]
    row = await task_states.get_by_id(db, "t-retained-001")
    outputs = json.loads(row["outputs"])
    assert row["current_phase"] == "cancelled"
    mock_invoker.run.assert_not_awaited()
    assert await worktree_mgr.is_registered_worktree(wt, repo)
    assert "worktree_path" not in outputs and not engine._worktree_paths
