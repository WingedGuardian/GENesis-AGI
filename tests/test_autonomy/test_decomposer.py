"""Tests for genesis.autonomy.decomposer.TaskDecomposer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.decomposer import TaskDecomposer, has_deliverable_frame
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


    async def test_resource_fields_preserved(self) -> None:
        """New optional resource fields are preserved during validation."""
        steps_with_resources = json.dumps([
            {
                "idx": 0,
                "type": "research",
                "description": "Research with skill",
                "required_tools": ["WebSearch"],
                "skills": ["research"],
                "procedures": ["web-search-pattern"],
                "mcp_guidance": ["memory"],
            },
            {
                "idx": 1,
                "type": "code",
                "description": "Code step",
                "required_tools": ["Write"],
            },
        ])
        router = _make_router(steps_with_resources)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task with resources")

        # Step 0 has resources
        assert steps[0]["skills"] == ["research"]
        assert steps[0]["procedures"] == ["web-search-pattern"]
        assert steps[0]["mcp_guidance"] == ["memory"]

        # Step 1 has empty defaults
        assert steps[1]["skills"] == []
        assert steps[1]["procedures"] == []
        assert steps[1]["mcp_guidance"] == []

    async def test_deterministic_step_with_command_preserved(self) -> None:
        """Deterministic steps with a command field are preserved."""
        steps_json = json.dumps([
            {
                "idx": 0,
                "type": "bash",
                "description": "Lint the code",
                "command": "ruff check src/",
            },
            {
                "idx": 1,
                "type": "test",
                "description": "Run tests",
                "command": "pytest tests/test_foo.py -v",
                "dependencies": [0],
            },
        ])
        router = _make_router(steps_json)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Lint and test")

        assert steps[0]["type"] == "bash"
        assert steps[0]["command"] == "ruff check src/"
        assert steps[1]["type"] == "test"
        assert steps[1]["command"] == "pytest tests/test_foo.py -v"

    async def test_deterministic_step_without_command_falls_back_to_code(self) -> None:
        """Deterministic steps missing command fall back to 'code'."""
        steps_json = json.dumps([
            {
                "idx": 0,
                "type": "bash",
                "description": "Do something",
                # no command field
            },
        ])
        router = _make_router(steps_json)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task")

        assert steps[0]["type"] == "code"
        assert "command" not in steps[0]

    async def test_shell_syntax_commands_fall_back_to_code(self) -> None:
        """Exec-only runner can't run shell constructs — downgrade upfront.

        Exactly the V0-canary failure shapes: 'source venv && pytest' and
        'git add X && git commit' died at runtime and burned CC recovery
        cycles; validation now catches them at decompose time.
        """
        steps_json = json.dumps([
            {"idx": 0, "type": "test", "description": "t",
             "command": "source /home/u/.venv/bin/activate && pytest tests/x.py"},
            {"idx": 1, "type": "git", "description": "g",
             "command": 'git add a.py b.py && git commit -m "feat: x"'},
            {"idx": 2, "type": "bash", "description": "b",
             "command": "pytest tests/x.py | tail -5"},
            {"idx": 3, "type": "bash", "description": "e",
             "command": "FOO=bar pytest tests/x.py"},
            {"idx": 4, "type": "bash", "description": "i",
             "command": "python -c 'print(1)'"},
            {"idx": 5, "type": "bash", "description": "cd",
             "command": "cd /somewhere"},
        ])
        router = _make_router(steps_json)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task")

        for step in steps[:6]:
            assert step["type"] == "code", step
            assert "command" not in step, step

    async def test_clean_exec_commands_stay_deterministic(self) -> None:
        """Plain single-program commands are untouched by the shell check."""
        steps_json = json.dumps([
            {"idx": 0, "type": "test", "description": "t",
             "command": "pytest tests/test_foo.py -v"},
            {"idx": 1, "type": "bash", "description": "r",
             "command": "ruff check src/"},
            {"idx": 2, "type": "git", "description": "g",
             "command": 'git commit -m "feat: wire A && B"'},
        ])
        router = _make_router(steps_json)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task")

        assert steps[0]["type"] == "test"
        assert steps[1]["type"] == "bash"
        # NOTE: '&&' inside a quoted commit message still trips the substring
        # check — acceptable false positive (downgrades to code, never breaks).
        assert steps[2]["type"] == "code"

    async def test_git_step_type_valid(self) -> None:
        """Git step type is accepted."""
        steps_json = json.dumps([
            {
                "idx": 0,
                "type": "git",
                "description": "Commit changes",
                "command": "git commit -m 'feat: add feature'",
            },
        ])
        router = _make_router(steps_json)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task")

        assert steps[0]["type"] == "git"
        assert steps[0]["command"] == "git commit -m 'feat: add feature'"

    async def test_resource_fields_invalid_types_default_empty(self) -> None:
        """Non-list resource fields default to empty lists."""
        steps_bad = json.dumps([
            {
                "idx": 0,
                "type": "code",
                "description": "Step",
                "skills": "not-a-list",
                "procedures": 42,
            },
        ])
        router = _make_router(steps_bad)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan", "Task")

        assert steps[0]["skills"] == []
        assert steps[0]["procedures"] == []
        assert steps[0]["mcp_guidance"] == []


FRAME_PLAN = """# Task

## Requirements
Produce a one-page brief.

## Deliverable Frame
- format: PDF
- visual_style: modern
- authenticity_target: AI-assisted-OK
- audience: hiring team
- acceptance: leads with the recommendation

## Steps
Draft and render.
"""

TWO_STEPS_JSON = json.dumps([
    {"idx": 0, "type": "research", "description": "Research"},
    {"idx": 1, "type": "code", "description": "Draft"},
])


@pytest.mark.asyncio
class TestDeliverableStepAppend:
    """Deterministic deliverable-builder step append (v2). Frame-gated."""

    async def test_frame_present_appends_terminal_deliverable_step(self) -> None:
        router = _make_router(TWO_STEPS_JSON)
        d = TaskDecomposer(router=router)
        steps = await d.decompose(FRAME_PLAN, "Make a brief")

        last = steps[-1]
        assert last["type"] == "synthesis"
        assert "deliverable-builder" in last["skills"]
        # exactly one deliverable-builder step, and it is terminal
        assert sum("deliverable-builder" in (s.get("skills") or []) for s in steps) == 1

    async def test_strips_trailing_autoverification(self) -> None:
        # _validate_steps appends a generic verification; the deliverable
        # append must strip it so the deliverable step is terminal.
        router = _make_router(TWO_STEPS_JSON)
        d = TaskDecomposer(router=router)
        steps = await d.decompose(FRAME_PLAN, "Make a brief")

        descs = [s["description"] for s in steps]
        assert "Verify deliverable against success criteria" not in descs
        assert steps[-1]["skills"] == ["deliverable-builder"]

    async def test_no_frame_no_append(self) -> None:
        router = _make_router(TWO_STEPS_JSON)
        d = TaskDecomposer(router=router)
        steps = await d.decompose("plan without a frame", "Do something")

        assert all("deliverable-builder" not in (s.get("skills") or []) for s in steps)
        # unchanged behavior: trailing generic verification still appended
        assert steps[-1]["type"] == "verification"

    async def test_no_double_append_when_llm_already_placed_it_last(self) -> None:
        steps_with_db = json.dumps([
            {"idx": 0, "type": "research", "description": "Research"},
            {"idx": 1, "type": "synthesis", "description": "Build deliverable",
             "skills": ["deliverable-builder"]},
        ])
        router = _make_router(steps_with_db)
        d = TaskDecomposer(router=router)
        steps = await d.decompose(FRAME_PLAN, "Make a brief")

        assert sum("deliverable-builder" in (s.get("skills") or []) for s in steps) == 1
        assert steps[-1]["skills"] == ["deliverable-builder"]

    async def test_appended_step_instructs_full_skill_read(self) -> None:
        router = _make_router(TWO_STEPS_JSON)
        d = TaskDecomposer(router=router)
        steps = await d.decompose(FRAME_PLAN, "Make a brief")

        last = steps[-1]
        desc = last["description"]
        assert "deliverable-builder" in desc.lower()
        # must tell the session the injected copy is partial -> read the complete
        # skill files (SKILL.md + references), since the Skill tool can't load it
        assert "complete skill" in desc.lower()
        assert "SKILL.md" in desc
        assert "references" in desc.lower()
        assert last["dependencies"] == [len(steps) - 2]  # depends on prior step

    async def test_fallback_path_also_appends_when_frame_present(self) -> None:
        # Total decomposition failure -> single_step_fallback; frame still present.
        router = _make_router(None, success=False)
        d = TaskDecomposer(router=router)
        steps = await d.decompose(FRAME_PLAN, "Make a brief")

        assert any("deliverable-builder" in (s.get("skills") or []) for s in steps)

    async def test_no_double_append_when_llm_placed_it_mid_plan(self) -> None:
        # LLM mis-places the deliverable step in the MIDDLE. The idempotency
        # guard must still prevent a second append (skill would run twice).
        steps_mid = json.dumps([
            {"idx": 0, "type": "research", "description": "Research"},
            {"idx": 1, "type": "synthesis", "description": "Build deliverable",
             "skills": ["deliverable-builder"]},
            {"idx": 2, "type": "code", "description": "More work after it"},
        ])
        router = _make_router(steps_mid)
        d = TaskDecomposer(router=router)
        steps = await d.decompose(FRAME_PLAN, "Make a brief")

        assert sum("deliverable-builder" in (s.get("skills") or []) for s in steps) == 1

    async def test_full_plan_embedded_in_step_description(self) -> None:
        # build_step_prompt doesn't pass the plan to steps, so the frame +
        # requirements must be embedded in the deliverable step's description.
        router = _make_router(TWO_STEPS_JSON)
        d = TaskDecomposer(router=router)
        steps = await d.decompose(FRAME_PLAN, "Make a brief")

        desc = steps[-1]["description"]
        assert "## Deliverable Frame" in desc
        assert "visual_style: modern" in desc
        assert "## Requirements" in desc  # substance reaches the session too


class TestHasDeliverableFrame:
    def test_detects_heading_any_level(self) -> None:
        assert has_deliverable_frame("# T\n## Deliverable Frame\n- format: PDF")
        assert has_deliverable_frame("### deliverable frame\nstuff")  # case-insensitive

    def test_absent(self) -> None:
        assert not has_deliverable_frame("# Task\n## Requirements\nstuff")
        assert not has_deliverable_frame("")
        assert not has_deliverable_frame(None)  # type: ignore[arg-type]

    def test_ignores_prose_mention(self) -> None:
        # A casual prose mention is not a heading -> no false positive.
        assert not has_deliverable_frame("We discussed the deliverable frame in the call.")


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
