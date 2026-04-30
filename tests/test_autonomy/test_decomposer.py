"""Tests for genesis.autonomy.decomposer.TaskDecomposer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.decomposer import TaskDecomposer
from genesis.db.crud.task_states import create_intake_token


@dataclass
class FakeRoutingResult:
    success: bool
    content: str | None = None
    error: str | None = None


def _make_router(response_content: str | None, success: bool = True) -> AsyncMock:
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=FakeRoutingResult(success=success, content=response_content)
    )
    return router


VALID_STEPS_JSON = json.dumps([
    {
        "idx": 0,
        "type": "research",
        "description": "Research the API",
        "required_tools": ["WebSearch", "WebFetch"],
        "complexity": "low",
        "dependencies": [],
    },
    {
        "idx": 1,
        "type": "code",
        "description": "Implement the endpoint",
        "required_tools": ["Write", "Edit", "Bash"],
        "complexity": "medium",
        "dependencies": [0],
    },
    {
        "idx": 2,
        "type": "verification",
        "description": "Run tests and verify",
        "required_tools": ["Bash"],
        "complexity": "low",
        "dependencies": [1],
    },
])


@pytest.mark.asyncio
class TestDecompose:
    async def test_valid_response_parsed(self) -> None:
        router = _make_router(VALID_STEPS_JSON)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan content", "Build an API")

        assert len(steps) == 3
        assert steps[0]["type"] == "research"
        assert steps[1]["type"] == "code"
        assert steps[2]["type"] == "verification"

        # Router called with correct call site
        router.route_call.assert_awaited_once()
        call_args = router.route_call.call_args
        assert call_args[0][0] == "27_pre_execution_assessment"

    async def test_route_failure_falls_back(self) -> None:
        router = _make_router(None, success=False)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Do something")

        assert len(steps) == 1
        assert steps[0]["type"] == "verification"
        assert "could not be decomposed" in steps[0]["description"]

    async def test_empty_content_falls_back(self) -> None:
        router = _make_router("")
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Do something")

        assert len(steps) == 1
        assert steps[0]["type"] == "verification"

    async def test_invalid_json_falls_back(self) -> None:
        router = _make_router("not json at all")
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Do something")

        assert len(steps) == 1
        assert steps[0]["type"] == "verification"

    async def test_code_fence_stripped(self) -> None:
        fenced = f"```json\n{VALID_STEPS_JSON}\n```"
        router = _make_router(fenced)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Build an API")

        assert len(steps) == 3

    async def test_max_steps_capped(self) -> None:
        many_steps = json.dumps([
            {"idx": i, "type": "code", "description": f"Step {i}"}
            for i in range(15)
        ])
        router = _make_router(many_steps)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Big task")

        # 8 steps max + 1 verification appended = 9 max, but capped at 8 before append
        # So: 8 code steps + 1 verification = 9... but we only keep 8 from the input,
        # then append verification if last isn't verification -> 9 total.
        # Wait, _MAX_STEPS is 8, and we check len < _MAX_STEPS before appending.
        # 8 steps, last is "code", 8 < 8 is False, so no append. Final: 8.
        assert len(steps) == 8

    async def test_verification_appended_if_missing(self) -> None:
        steps_no_verify = json.dumps([
            {"idx": 0, "type": "research", "description": "Research"},
            {"idx": 1, "type": "code", "description": "Code"},
        ])
        router = _make_router(steps_no_verify)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task")

        assert len(steps) == 3
        assert steps[-1]["type"] == "verification"
        assert steps[-1]["dependencies"] == [1]

    async def test_invalid_step_type_normalized(self) -> None:
        bad_type = json.dumps([
            {"idx": 0, "type": "INVALID_TYPE", "description": "Something"},
        ])
        router = _make_router(bad_type)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task")

        # Invalid type defaults to "code", then verification appended
        assert steps[0]["type"] == "code"

    async def test_dependencies_filtered_to_prior_only(self) -> None:
        """Forward dependencies (cycles) are filtered out."""
        cyclic = json.dumps([
            {"idx": 0, "type": "code", "description": "A", "dependencies": [1]},
            {"idx": 1, "type": "code", "description": "B", "dependencies": [0]},
        ])
        router = _make_router(cyclic)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task")

        # Step 0 can't depend on step 1 (forward reference)
        assert steps[0]["dependencies"] == []
        # Step 1 can depend on step 0 (valid back reference)
        assert steps[1]["dependencies"] == [0]


@pytest.mark.asyncio
class TestDecomposeDB:
    """Tests for task_steps and task_states CRUD additions."""

    async def test_task_steps_create_and_get(self, db) -> None:
        from genesis.db.crud import task_states, task_steps

        token = await create_intake_token(db)
        await task_states.create(db, task_id="t1", description="test task", intake_token=token)

        await task_steps.create_step(
            db, task_id="t1", step_idx=0, step_type="code", description="Write code"
        )
        await task_steps.create_step(
            db, task_id="t1", step_idx=1, step_type="verification", description="Verify"
        )

        steps = await task_steps.get_steps_for_task(db, "t1")
        assert len(steps) == 2
        assert steps[0]["step_idx"] == 0
        assert steps[0]["step_type"] == "code"
        assert steps[1]["step_idx"] == 1

    async def test_task_steps_update(self, db) -> None:
        from genesis.db.crud import task_states, task_steps

        token = await create_intake_token(db)
        await task_states.create(db, task_id="t1", description="test", intake_token=token)
        await task_steps.create_step(db, task_id="t1", step_idx=0)

        updated = await task_steps.update_step(
            db, "t1", 0, status="completed", cost_usd=0.05, model_used="claude-sonnet"
        )
        assert updated is True

        steps = await task_steps.get_steps_for_task(db, "t1")
        assert steps[0]["status"] == "completed"
        assert steps[0]["cost_usd"] == 0.05
        assert steps[0]["model_used"] == "claude-sonnet"

    async def test_get_last_completed_step(self, db) -> None:
        from genesis.db.crud import task_states, task_steps

        token = await create_intake_token(db)
        await task_states.create(db, task_id="t1", description="test", intake_token=token)
        for i in range(3):
            await task_steps.create_step(db, task_id="t1", step_idx=i)

        # Complete steps 0 and 1
        await task_steps.update_step(db, "t1", 0, status="completed")
        await task_steps.update_step(db, "t1", 1, status="completed")

        last = await task_steps.get_last_completed_step(db, "t1")
        assert last is not None
        assert last["step_idx"] == 1

    async def test_get_last_completed_step_none(self, db) -> None:
        from genesis.db.crud import task_states, task_steps

        token = await create_intake_token(db)
        await task_states.create(db, task_id="t1", description="test", intake_token=token)
        await task_steps.create_step(db, task_id="t1", step_idx=0)

        last = await task_steps.get_last_completed_step(db, "t1")
        assert last is None

    async def test_task_states_list_by_phase(self, db) -> None:
        from genesis.db.crud import task_states

        t1 = await create_intake_token(db)
        t2 = await create_intake_token(db)
        t3 = await create_intake_token(db)
        await task_states.create(db, task_id="t1", description="a", current_phase="executing", intake_token=t1)
        await task_states.create(db, task_id="t2", description="b", current_phase="completed", intake_token=t2)
        await task_states.create(db, task_id="t3", description="c", current_phase="executing", intake_token=t3)

        rows = await task_states.list_by_phase(db, "executing")
        assert len(rows) == 2
        assert all(r["current_phase"] == "executing" for r in rows)

    async def test_task_states_list_active(self, db) -> None:
        from genesis.db.crud import task_states

        t1 = await create_intake_token(db)
        t2 = await create_intake_token(db)
        t3 = await create_intake_token(db)
        t4 = await create_intake_token(db)
        await task_states.create(db, task_id="t1", description="a", current_phase="executing", intake_token=t1)
        await task_states.create(db, task_id="t2", description="b", current_phase="completed", intake_token=t2)
        await task_states.create(db, task_id="t3", description="c", current_phase="failed", intake_token=t3)
        await task_states.create(db, task_id="t4", description="d", current_phase="paused", intake_token=t4)

        active = await task_states.list_active(db)
        ids = {r["task_id"] for r in active}
        assert ids == {"t1", "t4"}

    async def test_task_states_list_all_recent(self, db) -> None:
        from genesis.db.crud import task_states

        for i in range(5):
            _tok = await create_intake_token(db)
            await task_states.create(db, task_id=f"t{i}", description=f"task {i}", intake_token=_tok)

        recent = await task_states.list_all_recent(db, limit=3)
        assert len(recent) == 3

    async def test_create_step_idempotent(self, db) -> None:
        from genesis.db.crud import task_states, task_steps

        token = await create_intake_token(db)
        await task_states.create(db, task_id="t1", description="test", intake_token=token)
        await task_steps.create_step(db, task_id="t1", step_idx=0)
        # Second insert should not raise (INSERT OR IGNORE)
        await task_steps.create_step(db, task_id="t1", step_idx=0)

        steps = await task_steps.get_steps_for_task(db, "t1")
        assert len(steps) == 1
