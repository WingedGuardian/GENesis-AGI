"""Tests for the in-flight working-state session-start block (open_loops)."""

from __future__ import annotations

import pytest

from genesis.db.crud import task_states
from genesis.memory import open_loops
from genesis.memory.open_loops import build_inflight_block


async def _insert_task(db, task_id, description, phase="observing"):
    # task_states has an intake-token trigger — go through the crud path, not
    # a raw INSERT (see CLAUDE.md: never insert directly into task_states).
    token = await task_states.create_intake_token(db)
    await task_states.create(
        db,
        task_id=task_id,
        description=description,
        current_phase=phase,
        intake_token=token,
    )


@pytest.fixture
def empty_dirs(tmp_path):
    """A non-git repo_root + empty plans_dir → worktree/plan sections empty."""
    repo = tmp_path / "repo"
    plans = tmp_path / "plans"
    repo.mkdir()
    plans.mkdir()
    return repo, plans


async def test_all_empty_returns_empty_string(empty_db, empty_dirs):
    repo, plans = empty_dirs
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert block == ""


async def test_block_has_directive_header_and_no_divider(empty_db, empty_dirs):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "task0001", "Fix the widget")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert block.startswith("### In-flight state (for your recollection, not a report)")
    assert "never open a session by summarizing it" in block
    # No markdown horizontal-rule divider — caller folds under Essential Knowledge.
    assert "\n---" not in block
    assert not block.startswith("---")


async def test_active_tasks_render(empty_db, empty_dirs):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "abcdef12", "Refactor the dispatcher", phase="planning")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Active autonomy tasks" in block
    assert "abcdef12" in block
    assert "planning" in block
    assert "Refactor the dispatcher" in block


async def test_terminal_tasks_excluded(empty_db, empty_dirs):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "done0001", "Completed thing", phase="completed")
    await _insert_task(empty_db, "live0001", "Live thing", phase="observing")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "live0001" in block
    assert "done0001" not in block


async def test_task_cap_and_overflow(empty_db, empty_dirs):
    repo, plans = empty_dirs
    for i in range(7):
        await _insert_task(empty_db, f"task{i:04d}", f"Task number {i}")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert block.count("- `") == 5  # 5 task bullets; overflow marker has no backtick
    assert "(+2 more)" in block


async def test_long_description_truncated(empty_db, empty_dirs):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "long0001", "x" * 200)
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "..." in block
    for line in block.splitlines():
        if line.startswith("- `"):  # a task bullet
            assert len(line) < 140  # no runaway 200-char task line


async def test_recent_plans_render(empty_db, empty_dirs):
    repo, plans = empty_dirs
    (plans / "my-plan.md").write_text("# plan")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Recent plans" in block
    assert "my-plan.md" in block


async def test_plan_cap(empty_db, empty_dirs):
    repo, plans = empty_dirs
    for i in range(5):
        (plans / f"plan-{i}.md").write_text("x")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    plan_section = block.split("**Recent plans:**")[1]
    assert plan_section.count("- ") == 3


async def test_worktrees_render_via_monkeypatch(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    monkeypatch.setattr(open_loops, "_list_worktrees", lambda root: [
        {"path": str(repo / "wt-a"), "head": "1234567890abcdef", "branch": "feat/foo"},
        {"path": str(repo / "wt-b"), "head": "abcdef1234567890"},  # detached (no branch)
    ])
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Live worktrees" in block
    assert "feat/foo @ 12345678" in block
    assert "wt-b @ abcdef12" in block  # detached → path basename


async def test_worktree_cap_and_overflow(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    many = [
        {"path": str(repo / f"wt{i}"), "head": f"{i:040d}", "branch": f"b{i}"}
        for i in range(12)
    ]
    monkeypatch.setattr(open_loops, "_list_worktrees", lambda root: many)
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "(+4 more)" in block  # 12 - 8 cap


async def test_one_section_raises_others_still_render(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "task0001", "survivor task")

    def boom(root):
        raise RuntimeError("worktree listing blew up")

    monkeypatch.setattr(open_loops, "_worktree_lines", boom)
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "survivor task" in block  # tasks survive the worktree failure
    assert "Live worktrees" not in block


async def test_truncation_hard_cap(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    monkeypatch.setattr(
        open_loops, "_plan_lines", lambda d: ["- " + "y" * 100 for _ in range(60)]
    )
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert len(block) <= open_loops._MAX_CHARS + 20
    assert "…(truncated)" in block
