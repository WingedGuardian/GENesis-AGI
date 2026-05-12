"""Tests for genesis.autonomy.executor.engine.CCSessionExecutor."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.autonomy.executor.engine import MAX_REVIEW_ITERATIONS, CCSessionExecutor
from genesis.autonomy.executor.review import ReviewResult, VerifyResult
from genesis.autonomy.executor.types import (
    StepResult,
    TaskPhase,
    WorkaroundResult,
)
from genesis.db.crud import task_states, task_steps
from genesis.db.crud.task_states import create_intake_token
from genesis.db.schema import create_all_tables

# ---------------------------------------------------------------------------
# Fake CC output (mirrors genesis.cc.types.CCOutput)
# ---------------------------------------------------------------------------


@dataclass
class FakeCCOutput:
    session_id: str = "sess-001"
    text: str = '{"status": "completed", "result": "done", "artifacts": []}'
    model_used: str = "sonnet"
    cost_usd: float = 0.01
    input_tokens: int = 100
    output_tokens: int = 50
    duration_ms: int = 1000
    exit_code: int = 0
    is_error: bool = False
    error_message: str | None = None
    model_requested: str = ""
    downgraded: bool = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def plan_file(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text("# Plan\n## Steps\n- Do X\n## Success Criteria\n- It works\n")
    return str(p)


@pytest.fixture
def mock_invoker():
    invoker = AsyncMock()
    invoker.run = AsyncMock(return_value=FakeCCOutput())
    return invoker


@pytest.fixture
def mock_decomposer():
    d = AsyncMock()
    d.decompose = AsyncMock(return_value=[
        {"idx": 0, "type": "research", "description": "Research", "complexity": "low", "dependencies": []},
        {"idx": 1, "type": "code", "description": "Implement", "complexity": "medium", "dependencies": [0]},
        {"idx": 2, "type": "verification", "description": "Verify", "complexity": "low", "dependencies": [1]},
    ])
    return d


@pytest.fixture
def mock_reviewer():
    r = AsyncMock()
    r.review_plan = AsyncMock(return_value=ReviewResult(passed=True))
    r.pre_mortem = AsyncMock(return_value=None)  # fail-open by default
    r.verify_deliverable = AsyncMock(return_value=VerifyResult(passed=True))
    return r


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Mock asyncio.create_subprocess_exec to avoid real git operations."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    create = AsyncMock(return_value=proc)
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    return create


async def _seed_task(db, task_id="t-001", plan_path="/tmp/plan.md", phase="pending"):
    """Insert a task row for testing."""
    token = await create_intake_token(db)
    await task_states.create(
        db,
        task_id=task_id,
        description="Test task",
        current_phase=phase,
        outputs=plan_path,
        intake_token=token,
    )


def _make_engine(db, invoker, decomposer, reviewer, **kwargs):
    """Factory for CCSessionExecutor with sensible defaults."""
    return CCSessionExecutor(
        db=db,
        invoker=invoker,
        decomposer=decomposer,
        reviewer=reviewer,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFullLifecycle:
    async def test_happy_path_pending_to_completed(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is True
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "completed"

    async def test_task_not_found_returns_false(
        self, db, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("nonexistent")
        assert result is False

    async def test_no_plan_path_returns_false(
        self, db, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        await _seed_task(db, plan_path="")
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")
        assert result is False

    async def test_unreadable_plan_returns_false(
        self, db, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        await _seed_task(db, plan_path="/nonexistent/plan.md")
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")
        assert result is False

    async def test_steps_persisted_to_db(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        await engine.execute("t-001")

        steps = await task_steps.get_steps_for_task(db, "t-001")
        assert len(steps) == 3
        assert all(s["status"] == "completed" for s in steps)


# ---------------------------------------------------------------------------
# State transitions (Amendment #10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStateTransitions:
    async def test_events_emitted_on_transitions(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        await _seed_task(db, plan_path=plan_file)
        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()
        engine = _make_engine(
            db, mock_invoker, mock_decomposer, mock_reviewer,
            event_bus=event_bus,
        )

        await engine.execute("t-001")

        # Should have emitted multiple phase_changed events
        emit_calls = event_bus.emit.call_args_list
        assert len(emit_calls) > 0

        # Check that at least REVIEWING and COMPLETED transitions emitted
        event_types = [c.args[2] for c in emit_calls if len(c.args) >= 3]
        assert "task.phase_changed" in event_types

    async def test_cleanup_runs_on_exception(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Engine cleans up active_tasks even on unexpected errors."""
        await _seed_task(db, plan_path=plan_file)
        # Make decomposer raise an unexpected error
        mock_decomposer.decompose = AsyncMock(side_effect=RuntimeError("boom"))
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is False
        assert "t-001" not in engine._active_tasks


# ---------------------------------------------------------------------------
# Amendment #1: Blocker persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAmendment1Blocker:
    async def test_blocker_persisted_before_notification(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Blocker must be saved to DB BEFORE outreach notification."""
        mock_reviewer.review_plan = AsyncMock(
            return_value=ReviewResult(
                passed=False, gaps=["Missing tests"],
            ),
        )

        # When outreach.submit is called, check that the blocker
        # is already persisted in the DB (proving DB-before-notify).
        blocker_in_db_at_notify_time = {"value": None}

        async def check_db_at_notify_time(request):
            task = await task_states.get_by_id(db, "t-001")
            blocker_in_db_at_notify_time["value"] = task.get("blockers")

        outreach = AsyncMock()
        outreach.submit = AsyncMock(side_effect=check_db_at_notify_time)

        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(
            db, mock_invoker, mock_decomposer, mock_reviewer,
            outreach_pipeline=outreach,
        )

        await engine.execute("t-001")

        # Blocker was already in DB when outreach fired
        assert blocker_in_db_at_notify_time["value"] is not None
        blocker = json.loads(blocker_in_db_at_notify_time["value"])
        assert "Missing tests" in blocker["description"]

    async def test_blocker_contains_resume_phase(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        mock_reviewer.review_plan = AsyncMock(
            return_value=ReviewResult(passed=False, gaps=["gap"]),
        )
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        await engine.execute("t-001")

        task = await task_states.get_by_id(db, "t-001")
        blocker = json.loads(task["blockers"])
        assert "resume_phase" in blocker
        assert blocker["resume_phase"] == "reviewing"


# ---------------------------------------------------------------------------
# Amendment #4: Review iteration cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAmendment4ReviewCap:
    async def test_escalates_after_max_failed_reviews(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """After MAX_REVIEW_ITERATIONS failures, escalate via blocker."""
        mock_reviewer.verify_deliverable = AsyncMock(
            return_value=VerifyResult(
                passed=False,
                fresh_eyes_feedback="Issues found",
                adversarial_feedback="Flaws detected",
            ),
        )
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is False
        # Verify review was called MAX_REVIEW_ITERATIONS times
        assert mock_reviewer.verify_deliverable.call_count == MAX_REVIEW_ITERATIONS
        # Verify blocker persisted
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "blocked"
        assert "Review failed" in task["blockers"]

    async def test_passes_on_second_iteration(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """First review fails, second passes -> task completes."""
        call_count = {"n": 0}

        async def _verify(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return VerifyResult(
                    passed=False,
                    fresh_eyes_feedback="Fix the bug",
                )
            return VerifyResult(passed=True)

        mock_reviewer.verify_deliverable = AsyncMock(side_effect=_verify)
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is True
        assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Amendment #7: Worktree management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAmendment7Worktree:
    async def test_worktree_created_for_code_steps(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Worktree is created when decomposer returns CODE steps."""
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        await engine.execute("t-001")

        # Check that git worktree add was called
        calls = mock_subprocess.call_args_list
        add_calls = [
            c for c in calls
            if "worktree" in c.args and "add" in c.args
        ]
        assert len(add_calls) == 1

    async def test_no_worktree_for_research_only(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """No worktree when all steps are research."""
        mock_decomposer.decompose = AsyncMock(return_value=[
            {"idx": 0, "type": "research", "description": "Research", "complexity": "low", "dependencies": []},
            {"idx": 1, "type": "verification", "description": "Verify", "complexity": "low", "dependencies": [0]},
        ])
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        await engine.execute("t-001")

        # No worktree add or remove calls
        calls = mock_subprocess.call_args_list
        wt_calls = [c for c in calls if "worktree" in c.args]
        assert len(wt_calls) == 0

    async def test_worktree_cleaned_on_completion(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        await engine.execute("t-001")

        # Worktree should be removed from internal state after completion
        assert "t-001" not in engine._worktree_paths

    async def test_worktree_cleaned_on_failure(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Worktree cleanup runs even when task fails."""
        mock_invoker.run = AsyncMock(
            return_value=FakeCCOutput(is_error=True, error_message="crash"),
        )
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        await engine.execute("t-001")

        assert "t-001" not in engine._worktree_paths

    async def test_code_step_gets_working_dir(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """CODE steps should have working_dir set to worktree path."""
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        await engine.execute("t-001")

        # Check invoker was called with working_dir for the code step
        inv_calls = mock_invoker.run.call_args_list
        # Step 1 is type=code, so its invocation should have working_dir set
        code_invocations = [
            c.args[0] for c in inv_calls
            if c.args[0].working_dir is not None
        ]
        assert len(code_invocations) >= 1


# ---------------------------------------------------------------------------
# Amendment #13: Global pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAmendment13GlobalPause:
    async def test_global_pause_at_checkpoint(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """When runtime.paused is True, task pauses at checkpoint."""
        runtime = MagicMock()
        runtime.paused = True

        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(
            db, mock_invoker, mock_decomposer, mock_reviewer,
            runtime=runtime,
        )

        # Run execute in a task so we can interact with it
        async def run():
            return await engine.execute("t-001")

        task = asyncio.create_task(run())

        # Give the executor time to hit the pause
        await asyncio.sleep(0.3)

        # Task should be paused
        assert engine._active_tasks.get("t-001") == TaskPhase.PAUSED

        # Resume by setting the pause event
        pause_event = engine._pause_events.get("t-001")
        assert pause_event is not None
        runtime.paused = False
        pause_event.set()

        result = await asyncio.wait_for(task, timeout=5.0)
        assert result is True


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCancel:
    async def test_cancel_at_next_checkpoint(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        await _seed_task(db, plan_path=plan_file)

        step_count = {"n": 0}
        original_run = mock_invoker.run

        async def run_then_cancel(inv):
            step_count["n"] += 1
            result = await original_run(inv)
            if step_count["n"] == 1:
                # Cancel after first step completes
                engine.cancel_task("t-001")
            return result

        mock_invoker.run = AsyncMock(side_effect=run_then_cancel)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        assert result is False
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "cancelled"

    async def test_cancel_task_returns_false_for_unknown(
        self, db, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        assert engine.cancel_task("nonexistent") is False


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStepExecution:
    async def test_step_dispatched_via_cc(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        await engine.execute("t-001")

        # 3 steps = 3 invoker.run calls
        assert mock_invoker.run.call_count == 3

    async def test_step_failure_tries_workaround(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Failed step triggers workaround search, retries on success."""
        call_count = {"n": 0}

        async def _run(inv):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakeCCOutput(is_error=True, error_message="failed")
            return FakeCCOutput()

        mock_invoker.run = AsyncMock(side_effect=_run)

        # Only one step so the failure is on step 0
        mock_decomposer.decompose = AsyncMock(return_value=[
            {"idx": 0, "type": "research", "description": "Do X", "complexity": "low", "dependencies": []},
        ])

        workaround = AsyncMock()
        workaround.search = AsyncMock(
            return_value=WorkaroundResult(found=True, approach="Try different approach"),
        )

        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(
            db, mock_invoker, mock_decomposer, mock_reviewer,
            workaround_searcher=workaround,
        )

        result = await engine.execute("t-001")

        assert result is True
        workaround.search.assert_awaited_once()

    async def test_blocked_step_persists_blocker(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """CC output with status=blocked triggers blocker persistence."""
        mock_invoker.run = AsyncMock(
            return_value=FakeCCOutput(
                text='{"status": "blocked", "result": "need creds", "blocker_description": "Need API key"}',
            ),
        )
        mock_decomposer.decompose = AsyncMock(return_value=[
            {"idx": 0, "type": "code", "description": "Call API", "complexity": "low", "dependencies": []},
        ])

        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is False
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "blocked"
        blocker = json.loads(task["blockers"])
        assert "Need API key" in blocker["description"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_get_active_tasks_excludes_terminal(self):
        engine = CCSessionExecutor(
            db=MagicMock(), invoker=MagicMock(),
            decomposer=MagicMock(), reviewer=MagicMock(),
        )
        engine._active_tasks = {
            "a": TaskPhase.EXECUTING,
            "b": TaskPhase.COMPLETED,
            "c": TaskPhase.PAUSED,
        }

        active = engine.get_active_tasks()
        assert "a" in active
        assert "b" not in active
        assert "c" in active

    def test_resume_task_returns_false_for_unknown(self):
        engine = CCSessionExecutor(
            db=MagicMock(), invoker=MagicMock(),
            decomposer=MagicMock(), reviewer=MagicMock(),
        )
        assert engine.resume_task("nonexistent") is False

    def test_pause_task_returns_false(self):
        engine = CCSessionExecutor(
            db=MagicMock(), invoker=MagicMock(),
            decomposer=MagicMock(), reviewer=MagicMock(),
        )
        assert engine.pause_task("t-001") is False


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


class TestParseStepOutput:
    def test_json_in_backticks(self):
        text = 'Some text\n```json\n{"status": "completed", "result": "ok"}\n```\nMore text'
        result = CCSessionExecutor._parse_step_output(text)
        assert result["status"] == "completed"
        assert result["result"] == "ok"

    def test_json_on_last_line(self):
        text = 'Working on it...\n{"status": "completed", "result": "done"}'
        result = CCSessionExecutor._parse_step_output(text)
        assert result["status"] == "completed"

    def test_no_json_defaults_to_completed(self):
        text = "Just some plain text output"
        result = CCSessionExecutor._parse_step_output(text)
        assert result["status"] == "completed"
        assert "Just some plain" in result["result"]

    def test_empty_text(self):
        result = CCSessionExecutor._parse_step_output("")
        assert result["status"] == "completed"

    def test_blocked_status_parsed(self):
        text = '{"status": "blocked", "blocker_description": "Need access"}'
        result = CCSessionExecutor._parse_step_output(text)
        assert result["status"] == "blocked"
        assert result["blocker_description"] == "Need access"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_synthesize_deliverable(self):
        results = [
            StepResult(idx=0, status="completed", result="Found X"),
            StepResult(idx=1, status="failed", result="Error"),
            StepResult(idx=2, status="completed", result="Verified"),
        ]
        text = CCSessionExecutor._synthesize_deliverable(results)
        assert "Found X" in text
        assert "Error" not in text  # Failed steps excluded
        assert "Verified" in text

    def test_dominant_step_type(self):
        steps = [
            {"type": "code"},
            {"type": "code"},
            {"type": "research"},
        ]
        assert CCSessionExecutor._dominant_step_type(steps) == "code"

    def test_create_fixup_step(self):
        verify = VerifyResult(
            passed=False,
            fresh_eyes_feedback="Fix the imports",
            adversarial_feedback="Missing error handling",
        )
        fixup = CCSessionExecutor._create_fixup_step(verify, 5)
        assert fixup["idx"] == 5
        assert fixup["type"] == "code"
        assert "Fix the imports" in fixup["description"]
        assert "Missing error handling" in fixup["description"]
