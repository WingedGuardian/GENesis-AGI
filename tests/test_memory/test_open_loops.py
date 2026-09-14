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


def _board(rows, *, age_h=1.0, isolate=None):
    """Build a board cache payload with a controllable age."""
    from datetime import UTC, datetime, timedelta

    gen = (datetime.now(UTC) - timedelta(hours=age_h)).isoformat()
    return {"generated_at": gen, "worktrees": rows}


@pytest.fixture(autouse=True)
def _isolate_at_risk_state(tmp_path, monkeypatch):
    """Never read or write the operator's real at-risk state from a test.

    ``_newly_at_risk`` PERSISTS what it saw, so without this a test run would
    both consume and overwrite live session state — and the second test in a file
    would see the first test's branches as "already seen".
    """
    monkeypatch.setattr(
        open_loops, "_AT_RISK_SEEN", tmp_path / "at-risk-seen.json"
    )
    monkeypatch.setattr(open_loops, "_BOARD_CACHE", tmp_path / "absent-board.json")


async def test_at_risk_branches_are_named(empty_db, empty_dirs, monkeypatch):
    """The at-risk set is named individually — that is the whole point."""
    repo, plans = empty_dirs
    monkeypatch.setattr(open_loops, "_read_board", lambda: (
        [
            {"branch": "feat/foo", "state": "at_risk", "action": "none"},
            {"branch": "", "path": "/x/wt-b", "state": "at_risk", "action": "none"},
        ], 3.0,
    ))
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Unlanded work at risk" in block
    assert "feat/foo" in block
    assert "wt-b" in block  # no branch (detached) → path basename


async def test_merged_and_fresh_worktrees_are_invisible(empty_db, empty_dirs, monkeypatch):
    """Nothing about merged or fresh work needs a human, so it is not shown.

    This is the failure the redesign exists to fix: the old block listed every
    worktree and overflowed to "(+183 more)", which is unreadable and therefore
    unread.
    """
    repo, plans = empty_dirs
    monkeypatch.setattr(open_loops, "_read_board", lambda: (
        [
            {"branch": "feat/landed", "state": "reap_merged", "action": "trash"},
            {"branch": "feat/new", "state": "fresh", "action": "none"},
        ], 1.0,
    ))
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Unlanded work at risk" not in block
    assert "feat/landed" not in block
    assert "feat/new" not in block


async def test_only_new_arrivals_are_named_the_rest_are_counted(
    empty_db, empty_dirs, monkeypatch, tmp_path,
):
    """Second sighting of the same branch counts, but does not re-name it.

    A list that is identical every session stops being read; the CHANGE is the
    signal. Asserting the SECOND call is what proves the state round-trips —
    asserting only the first would pass with persistence entirely broken.
    """
    repo, plans = empty_dirs
    rows = [
        {"branch": f"b{i}", "state": "at_risk", "action": "none"} for i in range(3)
    ]
    monkeypatch.setattr(open_loops, "_read_board", lambda: (rows, 1.0))

    first = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "3 newly at risk" in first
    assert "b0" in first

    second = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "newly at risk" not in second, "an unchanged set must not re-announce"
    assert "3 more aging" in second


async def test_named_arrivals_are_capped_and_the_remainder_declared(
    empty_db, empty_dirs, monkeypatch,
):
    """A burst of arrivals is bounded, and the overflow is STATED, not dropped."""
    repo, plans = empty_dirs
    rows = [
        {"branch": f"b{i}", "state": "at_risk", "action": "none"} for i in range(9)
    ]
    monkeypatch.setattr(open_loops, "_read_board", lambda: (rows, 1.0))
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "9 newly at risk" in block
    assert "(+4 more new)" in block  # 9 - _MAX_NAMED_AT_RISK(5)


async def test_stale_board_refuses_to_name_branches(empty_db, empty_dirs, monkeypatch):
    """Past the staleness bound the cache describes a tree that has moved on.

    Reporting its branch names as current would be a confident lie, so the block
    says the board is stale and how to refresh it instead.
    """
    repo, plans = empty_dirs
    monkeypatch.setattr(open_loops, "_read_board", lambda: (
        [{"branch": "feat/ancient", "state": "at_risk", "action": "none"}], 200.0,
    ))
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "stale" in block
    assert "feat/ancient" not in block
    assert "--report-json" in block


async def test_missing_board_says_nothing(empty_db, empty_dirs, monkeypatch):
    """No cache is a real answer: emit nothing rather than guess or reassure."""
    repo, plans = empty_dirs
    monkeypatch.setattr(open_loops, "_read_board", lambda: ([], None))
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Unlanded work at risk" not in block


async def test_read_board_survives_a_corrupt_cache(empty_db, empty_dirs, tmp_path, monkeypatch):
    """A truncated or hand-edited cache degrades to "no board", never an exception."""
    for payload in ("", "{", "null", "[]", '{"worktrees": "not-a-list"}',
                    '{"worktrees": [], "generated_at": "not-a-date"}'):
        cache = tmp_path / "board.json"
        cache.write_text(payload)
        monkeypatch.setattr(open_loops, "_BOARD_CACHE", cache)
        rows, age = open_loops._read_board()
        assert isinstance(rows, list), f"payload {payload!r} did not degrade cleanly"


async def test_one_section_raises_others_still_render(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "task0001", "survivor task")

    def boom(root):
        raise RuntimeError("worktree listing blew up")

    monkeypatch.setattr(open_loops, "_worktree_lines", boom)
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "survivor task" in block  # tasks survive the worktree failure
    assert "Unlanded work at risk" not in block


async def test_truncation_hard_cap(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    monkeypatch.setattr(
        open_loops, "_plan_lines", lambda d: ["- " + "y" * 100 for _ in range(60)]
    )
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert len(block) <= open_loops._MAX_CHARS + 20
    assert "…(truncated)" in block
