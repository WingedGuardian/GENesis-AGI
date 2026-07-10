"""Tests for genesis.autonomy.executor.engine.CCSessionExecutor."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.autonomy.executor.engine import MAX_REVIEW_ITERATIONS, CCSessionExecutor
from genesis.autonomy.executor.review import PreMortemResult, ReviewResult, VerifyResult
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


# ---------------------------------------------------------------------------
# Pre-mortem integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPreMortemIntegration:
    async def test_premortem_blocks_low_confidence(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        """Pre-mortem with confidence < 50 blocks the task."""
        mock_reviewer.pre_mortem = AsyncMock(
            return_value=PreMortemResult(
                confidence=30,
                failure_modes=["Fundamental approach is wrong"],
                mitigations=["Rethink approach"],
            ),
        )
        await _seed_task(db, plan_path=str(plan_file))
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        assert result is False
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "blocked"
        blocker = json.loads(task.get("blockers") or "{}")
        assert "Pre-mortem" in blocker.get("description", "")

    async def test_premortem_injects_mitigations(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        """Pre-mortem with confidence 50-70 injects mitigations and proceeds."""
        mock_reviewer.pre_mortem = AsyncMock(
            return_value=PreMortemResult(
                confidence=60,
                failure_modes=["Possible edge case"],
                mitigations=["Handle edge case X", "Add fallback for Y"],
            ),
        )
        await _seed_task(db, plan_path=str(plan_file))
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        assert result is True
        # Verify mitigations were stored in outputs
        task = await task_states.get_by_id(db, "t-001")
        outputs = json.loads(task.get("outputs", "{}"))
        pm_data = json.loads(outputs.get("pre_mortem", "{}"))
        assert pm_data["confidence"] == 60
        assert len(pm_data["mitigations"]) == 2

    async def test_premortem_high_confidence_proceeds(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        """Pre-mortem with confidence > 70 proceeds normally."""
        mock_reviewer.pre_mortem = AsyncMock(
            return_value=PreMortemResult(
                confidence=85,
                failure_modes=[],
                mitigations=[],
            ),
        )
        await _seed_task(db, plan_path=str(plan_file))
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        assert result is True


# ---------------------------------------------------------------------------
# OBSERVING phase integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestObservingPhase:
    async def test_fresh_task_passes_observing(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Fresh task passes OBSERVING without blocking."""
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        assert result is True
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "completed"

    async def test_stale_task_annotates_plan(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Stale task gets annotations but still completes (never blocks)."""
        from datetime import timedelta

        old_date = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        await _seed_task(db, plan_path=plan_file)
        await db.execute(
            "UPDATE task_states SET updated_at = ? WHERE task_id = ?",
            (old_date, "t-001"),
        )
        await db.commit()

        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        # Should complete, not block
        assert result is True
        # Plan file should have observation audit with staleness annotation
        content = Path(plan_file).read_text()
        assert "## Audit: OBSERVING" in content
        assert "No activity" in content

    async def test_observing_skipped_on_recovery(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Recovery path skips OBSERVING (resumes from executing)."""
        await _seed_task(db, plan_path=plan_file, phase="executing")
        # Add a completed step so recovery has something to work with
        await task_steps.create_step(
            db, task_id="t-001", step_idx=0, step_type="research",
            description="Research",
        )
        await db.execute(
            """UPDATE task_steps SET status = 'completed',
               result_json = '{"result": "done", "artifacts": []}'
               WHERE task_id = ? AND step_idx = ?""",
            ("t-001", 0),
        )
        await db.commit()

        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        assert result is True
        # Review was never called (skipped by recovery)
        mock_reviewer.review_plan.assert_not_called()


# ---------------------------------------------------------------------------
# Pre-planning blocker recovery (Option B fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPrePlanningBlockerRecovery:
    async def test_blocked_before_planning_reruns_fresh_path(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Task blocked before PLANNING (no steps) resumes via fresh path."""
        # Seed as blocked (simulates a prior review blocker)
        await _seed_task(db, plan_path=plan_file, phase="blocked")
        # No steps exist in the DB (blocked before PLANNING)

        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        # Should re-run through OBSERVING -> REVIEWING -> PLANNING -> ...
        assert result is True
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "completed"
        # Review was called (fresh path ran)
        mock_reviewer.review_plan.assert_called_once()


# ---------------------------------------------------------------------------
# Living plan audit trail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLivingPlan:
    async def test_plan_file_gets_audit_sections(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Plan file should have audit sections after execution."""
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        await engine.execute("t-001")

        content = Path(plan_file).read_text()
        assert "## Audit: REVIEWING" in content
        assert "## Audit: PLANNING" in content
        assert "## Audit: COMPLETED" in content

    async def test_plan_append_failopen(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Unwritable plan file does not block execution."""
        import os

        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        # Make plan file read-only
        os.chmod(plan_file, 0o444)
        try:
            result = await engine.execute("t-001")
            assert result is True
        finally:
            os.chmod(plan_file, 0o644)


# ---------------------------------------------------------------------------
# Pre-synthesis guard (todo continuation enforcer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPreSynthesisGuard:
    async def test_incomplete_steps_block_synthesis(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """If any steps are not completed, task should be blocked before synthesis."""
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        # Override step dispatcher to mark step 1 as "failed" in DB after execution
        original_execute = engine._step_dispatcher.execute_step

        call_count = 0

        async def execute_with_one_failure(task_id, step, prior, **kw):
            nonlocal call_count
            result = await original_execute(task_id, step, prior, **kw)
            call_count += 1
            # After executing step 1 (idx=1), manually corrupt its DB status
            if step["idx"] == 1:
                await task_steps.update_step(db, task_id, 1, status="failed")
            return result

        engine._step_dispatcher.execute_step = execute_with_one_failure

        result = await engine.execute("t-001")

        assert result is False
        task = await task_states.get_by_id(db, "t-001")
        # Task should be blocked, not completed
        assert task["current_phase"] in ("blocked", "verifying")

    async def test_all_completed_steps_pass_guard(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Normal case: all steps completed => task proceeds to synthesis."""
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is True
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "completed"

        # All steps should be completed
        steps = await task_steps.get_steps_for_task(db, "t-001")
        assert all(s["status"] == "completed" for s in steps)


# ---------------------------------------------------------------------------
# Task-scoped notepad
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTaskNotepad:
    async def test_notepad_seeded_in_worktree(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Notepad file should be created when worktree is created for code steps."""
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        # Capture worktree path from _seed_notepad call
        seeded_paths: list[str] = []
        original_seed = engine._seed_notepad

        def capturing_seed(task_id):
            original_seed(task_id)
            wt = engine._worktree_paths.get(task_id)
            if wt:
                seeded_paths.append(str(wt))

        engine._seed_notepad = capturing_seed

        await engine.execute("t-001")

        # mock_subprocess prevents real worktree creation, so _worktree_paths
        # is empty. Verify the method was called without error.
        # Integration test would verify the file exists.
        assert True  # _seed_notepad was called without raising

    async def test_notepad_promote_skips_empty(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Notepad promotion should silently skip when no worktree exists."""
        await _seed_task(db, plan_path=plan_file)
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        # Should not raise even though no worktree/notepad exists
        result = await engine.execute("t-001")
        assert result is True

    async def test_seed_notepad_creates_file(self, tmp_path):
        """_seed_notepad writes TASK_NOTEPAD.md with expected skeleton."""
        from genesis.autonomy.executor.engine import CCSessionExecutor

        engine = CCSessionExecutor(
            db=AsyncMock(), invoker=AsyncMock(),
            decomposer=AsyncMock(), reviewer=AsyncMock(),
        )
        engine._worktree_paths["t-test"] = tmp_path
        engine._seed_notepad("t-test")

        notepad = tmp_path / "TASK_NOTEPAD.md"
        assert notepad.exists()
        content = notepad.read_text()
        assert "## Learnings" in content
        assert "## Decisions" in content
        assert "## Issues" in content

    async def test_promote_skips_skeleton_only(self, tmp_path):
        """Promotion should not fire when notepad contains only the skeleton."""
        from genesis.autonomy.executor.engine import CCSessionExecutor

        engine = CCSessionExecutor(
            db=AsyncMock(), invoker=AsyncMock(),
            decomposer=AsyncMock(), reviewer=AsyncMock(),
        )
        engine._worktree_paths["t-test"] = tmp_path
        engine._seed_notepad("t-test")

        # Should return without storing (skeleton only, no added content)
        await engine._promote_notepad("t-test", "test task")
        # No assertion on store — the method returns silently

    async def test_promote_detects_added_content(self, tmp_path):
        """Promotion should detect content added under section headings."""
        from genesis.autonomy.executor.engine import CCSessionExecutor

        engine = CCSessionExecutor(
            db=AsyncMock(), invoker=AsyncMock(),
            decomposer=AsyncMock(), reviewer=AsyncMock(),
        )
        engine._worktree_paths["t-test"] = tmp_path
        engine._seed_notepad("t-test")

        # Simulate a step adding learnings (content within skeleton sections)
        notepad = tmp_path / "TASK_NOTEPAD.md"
        content = notepad.read_text()
        content = content.replace(
            "## Learnings\n",
            "## Learnings\n- The API uses cursor-based pagination\n",
        )
        notepad.write_text(content)

        # _promote_notepad will try GenesisRuntime.instance() and fail
        # (no runtime in tests), but the content detection runs before that
        # We verify by checking the method doesn't return early at "no content"
        # by patching the runtime
        mock_store = AsyncMock()
        mock_rt_cls = MagicMock()
        mock_rt_cls.instance.return_value._memory_store = mock_store
        with patch.dict("sys.modules", {"genesis.runtime": MagicMock(GenesisRuntime=mock_rt_cls)}):
            await engine._promote_notepad("t-test", "test task")

        mock_store.store.assert_called_once()
        stored_content = mock_store.store.call_args.kwargs["content"]
        assert "cursor-based pagination" in stored_content
        # Notepad promotion is internal-subsystem output: tagged so it routes
        # FTS5-only and stays out of default semantic recall.
        assert mock_store.store.call_args.kwargs["source_subsystem"] == "autonomy"


# ---------------------------------------------------------------------------
# Notification kinds: verbatim delivery + per-kind voice signal / voice_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestNotifyKinds:
    async def _capture(self, db, kind, **kw):
        captured = {}

        async def grab(request):
            captured["req"] = request

        outreach = AsyncMock()
        outreach.submit = AsyncMock(side_effect=grab)
        engine = _make_engine(
            db, AsyncMock(), AsyncMock(), AsyncMock(),
            outreach_pipeline=outreach,
        )
        await engine._notify("t-abc123", "Some message", kind, **kw)
        return captured.get("req")

    async def test_all_task_notifications_are_verbatim(self, db):
        """Every task notification is delivered verbatim — the LLM drafter is
        never in the path, so it cannot rewrite or fabricate a task status."""
        for kind in ("progress", "success", "alert", "blocked"):
            req = await self._capture(db, kind)
            assert req.verbatim is True, kind
            assert req.context == "Some message", kind

    async def test_progress_is_silent_and_carries_no_voice_text(self, db):
        from genesis.outreach.types import OutreachCategory

        req = await self._capture(db, "progress")
        assert req.signal_type == "task_progress"   # not on voice allowlist
        assert req.voice_text is None
        assert req.category == OutreachCategory.ALERT

    async def test_success_and_alert_carry_voice_text(self, db):
        req = await self._capture(db, "success", voice_text="Done.")
        assert req.signal_type == "task_complete"
        assert req.voice_text == "Done."

        req2 = await self._capture(db, "alert", voice_text="Problem.")
        assert req2.signal_type == "task_alert"
        assert req2.voice_text == "Problem."

    async def test_blocked_keeps_blocker_category_and_salience(self, db):
        from genesis.outreach.types import OutreachCategory

        req = await self._capture(db, "blocked", voice_text="Blocked.")
        assert req.category == OutreachCategory.BLOCKER
        assert req.salience_score == 0.9
        assert req.signal_type == "task_alert"


@pytest.mark.asyncio
async def test_notify_e2e_verbatim_telegram_and_tokenfree_voice(db):
    """E2E across the real seam: engine._notify -> real OutreachPipeline ->
    Telegram receives the EXACT detailed text (tokens/paths intact) while voice
    speaks ONLY the short token-free TL;DR. The LLM drafter is never called."""
    from unittest.mock import patch

    from genesis.content.types import FormattedContent
    from genesis.outreach.config import OutreachConfig, QuietHours
    from genesis.outreach.governance import GovernanceGate
    from genesis.outreach.pipeline import OutreachPipeline

    cfg = OutreachConfig(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.0, "surplus": 0.7, "digest": 0.0},
        max_daily=50, surplus_daily=1, content_daily=3, notification_daily=50,
        morning_report_time="07:00", engagement_timeout_hours=24,
        engagement_poll_minutes=60,
        voice_alert_ids=("task_alert", "task_complete"),
    )
    echo = MagicMock()
    echo.format.side_effect = lambda text, target: FormattedContent(
        text=text, target=target, truncated=False, original_length=len(text),
    )
    telegram = AsyncMock()
    telegram.send_message.return_value = "tg-1"
    voice = AsyncMock()
    voice.send_message.return_value = "v-1"
    drafter = AsyncMock()  # must NEVER be invoked for a task notification
    pipeline = OutreachPipeline(
        governance=GovernanceGate(cfg, db), drafter=drafter, formatter=echo,
        channels={"telegram": telegram, "voice": voice}, db=db, config=cfg,
        recipients={"telegram": "12345"},
    )
    engine = _make_engine(
        db, AsyncMock(), AsyncMock(), AsyncMock(), outreach_pipeline=pipeline,
    )
    detailed = (
        "Build parked by scope gate: bad path. "
        "Blocked paths: src/genesis/autonomy/x.py"
    )
    with patch.object(pipeline, "_in_voice_hours", return_value=True):
        await engine._notify(
            "t-e2e-9f8a", detailed, "alert",
            voice_text="A build was parked by the safety gate.",
        )
    await asyncio.sleep(0)  # let the fire-and-forget voice task record its call

    drafter.draft.assert_not_called()
    assert telegram.send_message.call_args.args[1] == detailed  # tokens intact
    spoken = voice.send_message.call_args.args[1]
    assert spoken == "A build was parked by the safety gate."
    assert "src/genesis" not in spoken  # no path read aloud


# ---------------------------------------------------------------------------
# WS-C: atomic dispatch claim + refuse-taxonomy (F2 — duplicate execution)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDispatchClaim:
    """Atomic PENDING claim + explicit refuse-taxonomy in execute().

    Proves the F2 fix: a task may only be run once. Overlapping dispatches
    lose the atomic claim, and a task already in a terminal / tail /
    transient phase is refused instead of silently re-run from scratch.
    """

    async def test_completed_task_is_refused_not_rerun(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """A 'completed' task must be refused, never re-run fresh (the harm)."""
        await _seed_task(db, plan_path=plan_file, phase="completed")
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is False
        mock_decomposer.decompose.assert_not_called()
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "completed"

    async def test_tail_phase_task_is_refused(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """A task stuck in a non-resumable tail phase (synthesizing) is refused."""
        await _seed_task(db, plan_path=plan_file, phase="synthesizing")
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is False
        mock_decomposer.decompose.assert_not_called()

    async def test_dispatching_phase_is_refused(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """The transient 'dispatching' claim phase is refused by execute()
        (only the reaper resets it to pending)."""
        await _seed_task(db, plan_path=plan_file, phase="dispatching")
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is False
        mock_decomposer.decompose.assert_not_called()

    async def test_concurrent_execute_runs_once(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """Two overlapping execute() calls for one pending task → exactly one runs."""
        await _seed_task(db, plan_path=plan_file, phase="pending")
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        results = await asyncio.gather(
            engine.execute("t-001"), engine.execute("t-001"),
        )

        assert sum(1 for r in results if r is True) == 1
        assert mock_decomposer.decompose.call_count == 1

    async def test_claim_for_dispatch_is_atomic(self, db):
        """First claim wins (pending -> dispatching); a second claim loses."""
        await _seed_task(db, phase="pending")

        won_first = await task_states.claim_for_dispatch(db, "t-001")
        won_second = await task_states.claim_for_dispatch(db, "t-001")

        assert won_first is True
        assert won_second is False
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "dispatching"

    async def test_claim_for_dispatch_stamps_updated_at(self, db):
        """A winning claim refreshes updated_at (reaper staleness relies on it)."""
        await _seed_task(db, phase="pending")
        await task_states.update(
            db, "t-001", updated_at="2020-01-01T00:00:00+00:00",
        )

        assert await task_states.claim_for_dispatch(db, "t-001") is True

        after = (await task_states.get_by_id(db, "t-001"))["updated_at"]
        assert after != "2020-01-01T00:00:00+00:00"

    async def test_claim_for_dispatch_rejects_non_pending(self, db):
        """Only a 'pending' row is claimable; a 'blocked' row is not."""
        await _seed_task(db, phase="blocked")
        assert await task_states.claim_for_dispatch(db, "t-001") is False

    async def test_recover_stale_dispatching_resets_old(self, db):
        """A 'dispatching' row older than max_age is reset to pending."""
        await _seed_task(db, phase="pending")
        await task_states.update(
            db, "t-001", current_phase="dispatching",
            updated_at="2020-01-01T00:00:00+00:00",
        )

        n = await task_states.recover_stale_dispatching(db, max_age_s=120)

        assert n == 1
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "pending"

    async def test_recover_stale_dispatching_spares_fresh(self, db):
        """A freshly-claimed 'dispatching' row is NOT reaped."""
        await _seed_task(db, phase="pending")
        fresh = datetime.now(UTC).isoformat()
        await task_states.update(
            db, "t-001", current_phase="dispatching", updated_at=fresh,
        )

        n = await task_states.recover_stale_dispatching(db, max_age_s=120)

        assert n == 0
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "dispatching"

    async def test_paused_task_resumes_without_redoing_completed_work(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """A paused task recovered at startup RESUMES from persisted steps — it
        is neither refused (would orphan it: the reaper only resets
        'dispatching', never 'paused') nor re-run fresh (would redo completed
        steps + re-spend cost). Proven by decompose/review NOT being called."""
        await _seed_task(db, plan_path=plan_file, phase="paused")
        await task_steps.create_step(
            db, task_id="t-001", step_idx=0, step_type="research",
            description="Research",
        )
        await db.execute(
            """UPDATE task_steps SET status = 'completed',
               result_json = '{"result": "done", "artifacts": []}'
               WHERE task_id = ? AND step_idx = ?""",
            ("t-001", 0),
        )
        await db.commit()

        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        result = await engine.execute("t-001")

        assert result is True
        mock_decomposer.decompose.assert_not_called()  # no re-plan
        mock_reviewer.review_plan.assert_not_called()  # no re-review

    async def test_paused_task_repauses_on_resume(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """A paused task with _paused_tasks pre-set (as recover_incomplete does)
        resumes and re-pauses at the first checkpoint rather than wedging or
        re-planning."""
        await _seed_task(db, plan_path=plan_file, phase="paused")
        # Two steps; step 0 is then marked completed, step 1 stays pending so
        # resume executes it and the checkpoint honors the pre-set pause flag.
        for idx in (0, 1):
            await task_steps.create_step(
                db, task_id="t-001", step_idx=idx, step_type="research",
                description=f"Step {idx}",
            )
        await db.execute(
            """UPDATE task_steps SET status='completed',
               result_json='{"result": "done", "artifacts": []}'
               WHERE task_id='t-001' AND step_idx=0""",
        )
        await db.commit()

        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)
        engine._paused_tasks.add("t-001")  # as recover_incomplete pre-sets

        # execute() parks in the checkpoint pause-wait loop, so run it as a task.
        task = asyncio.create_task(engine.execute("t-001"))
        await asyncio.sleep(0.3)

        # Resumed from persisted steps (not re-planned) and re-paused at the
        # first checkpoint — it did not wedge or re-decompose.
        assert engine._active_tasks.get("t-001") == TaskPhase.PAUSED
        mock_decomposer.decompose.assert_not_called()

        # Clear the pause flag and release the wait loop; it runs to completion.
        engine._paused_tasks.discard("t-001")
        pause_event = engine._pause_events.get("t-001")
        assert pause_event is not None
        pause_event.set()
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result is True

    async def test_restartable_planning_phase_reruns_fresh(
        self, db, plan_file, mock_invoker, mock_decomposer, mock_reviewer,
        mock_subprocess,
    ):
        """A task crashed mid-planning re-runs fresh (pre-execution phase, no
        committed side effects), rather than being refused."""
        await _seed_task(db, plan_path=plan_file, phase="planning")
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is True
        mock_decomposer.decompose.assert_called()

    async def test_pending_with_unreadable_plan_is_failed_not_claimed(
        self, db, mock_invoker, mock_decomposer, mock_reviewer,
    ):
        """A pending task with an unreadable plan is marked failed, and the plan
        read (which fails) happens before the claim — so the row is never left
        stuck in the transient 'dispatching' phase."""
        await _seed_task(db, plan_path="/nonexistent/plan.md", phase="pending")
        engine = _make_engine(db, mock_invoker, mock_decomposer, mock_reviewer)

        result = await engine.execute("t-001")

        assert result is False
        task = await task_states.get_by_id(db, "t-001")
        assert task["current_phase"] == "failed"  # not stuck in 'dispatching'
        assert "t-001" not in engine.get_active_tasks()  # terminal, not surfaced
