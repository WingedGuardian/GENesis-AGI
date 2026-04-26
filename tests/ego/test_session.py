"""Tests for the ego session orchestrator."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.cc.types import CCOutput
from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.dispatch import EgoDispatcher
from genesis.ego.session import EgoSession
from genesis.ego.types import CycleType, EgoConfig

# ---------------------------------------------------------------------------
# Sample ego output
# ---------------------------------------------------------------------------

def _valid_output(
    *,
    proposals: list | None = None,
    focus: str = "investigating backlog growth",
    follow_ups: list | None = None,
    morning_report: str | None = None,
) -> str:
    """Build a valid ego JSON output string."""
    default_proposals = [
        {
            "action_type": "investigate",
            "action_category": "system_health",
            "content": "Check observation backlog growth",
            "rationale": "Backlog at 47 vs 15 yesterday",
            "confidence": 0.85,
            "urgency": "normal",
            "alternatives": "Wait for reflection to catch it",
        },
    ]
    data = {
        "proposals": proposals if proposals is not None else default_proposals,
        "focus_summary": focus,
        "follow_ups": follow_ups if follow_ups is not None else ["check backlog tomorrow"],
    }
    if morning_report is not None:
        data["morning_report"] = morning_report
    return json.dumps(data)


def _cc_output(text: str = "", *, is_error: bool = False, cost: float = 0.15) -> CCOutput:
    """Build a mock CCOutput."""
    return CCOutput(
        session_id="sess123",
        text=text or _valid_output(),
        model_used="opus",
        cost_usd=cost,
        input_tokens=5000,
        output_tokens=1000,
        duration_ms=30000,
        exit_code=0 if not is_error else 1,
        is_error=is_error,
        error_message="CC failed" if is_error else None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """In-memory DB with ego tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for table in ("ego_cycles", "ego_proposals", "ego_state", "follow_ups"):
            await conn.execute(TABLES[table])
        yield conn


@pytest.fixture
def mock_invoker():
    invoker = AsyncMock()
    invoker.run.return_value = _cc_output()
    return invoker


@pytest.fixture
def mock_session_manager():
    sm = AsyncMock()
    sm.create_background.return_value = {"id": "bg_sess_1"}
    return sm


@pytest.fixture
def mock_compaction(db):
    comp = AsyncMock()
    comp.assemble_context.return_value = "# Context\nTest operational context"
    comp.store_cycle.return_value = "cycle_stored"
    return comp


@pytest.fixture
def mock_context_builder():
    cb = AsyncMock()
    cb.build.return_value = "# Fresh Context"
    return cb


@pytest.fixture
def mock_proposal_workflow():
    pw = AsyncMock()
    pw.create_batch.return_value = ("batch123", ["p1"])
    pw.send_digest.return_value = "msg456"
    return pw


@pytest.fixture
def dispatcher(db):
    return EgoDispatcher(db=db)


@pytest.fixture
def config():
    return EgoConfig(daily_budget_cap_usd=10.0)


@pytest.fixture
def ego_session(
    mock_invoker, mock_session_manager, mock_compaction,
    mock_context_builder, mock_proposal_workflow, dispatcher,
    config, db,
):
    return EgoSession(
        invoker=mock_invoker,
        session_manager=mock_session_manager,
        compaction_engine=mock_compaction,
        context_builder=mock_context_builder,
        proposal_workflow=mock_proposal_workflow,
        dispatcher=dispatcher,
        config=config,
        db=db,
        mcp_config_path=None,
    )


# ---------------------------------------------------------------------------
# Session cycle tests
# ---------------------------------------------------------------------------


class TestEgoSession:
    async def test_run_cycle_success(self, ego_session, mock_invoker, db):
        """Full successful cycle: proposals created, focus stored."""
        cycle = await ego_session.run_cycle()

        assert cycle is not None
        assert cycle.cost_usd == 0.15
        assert cycle.model_used == "opus"
        assert cycle.focus_summary == "investigating backlog growth"
        mock_invoker.run.assert_called_once()

        # Focus summary stored in ego_state
        focus = await ego_crud.get_state(db, "ego_focus_summary")
        assert focus == "investigating backlog growth"

    async def test_run_cycle_no_proposals(
        self, ego_session, mock_invoker, mock_proposal_workflow,
    ):
        """Cycle with empty proposals array still stores the cycle."""
        mock_invoker.run.return_value = _cc_output(
            _valid_output(proposals=[], follow_ups=[]),
        )
        cycle = await ego_session.run_cycle()

        assert cycle is not None
        mock_proposal_workflow.create_batch.assert_not_called()

    async def test_run_cycle_with_follow_ups(self, ego_session, dispatcher):
        """Follow_ups from output are recorded in ego_state."""
        cycle = await ego_session.run_cycle()
        assert cycle is not None

        pending = await dispatcher.get_pending_follow_ups()
        assert len(pending) == 1
        assert pending[0]["content"] == "check backlog tomorrow"

    async def test_run_cycle_morning_report(self, ego_session, mock_invoker):
        """Morning report flag appears in the user prompt."""
        await ego_session.run_cycle(is_morning_report=True)

        call_args = mock_invoker.run.call_args[0][0]
        assert "MORNING REPORT" in call_args.prompt

    async def test_run_cycle_budget_exceeded(
        self, ego_session, mock_invoker, db, config,
    ):
        """Cycle raises BudgetExceededError when budget is exceeded."""
        from genesis.ego.session import BudgetExceededError

        # Insert a costly cycle to exhaust budget
        await ego_crud.create_cycle(
            db, id="expensive", output_text="x",
            cost_usd=config.daily_budget_cap_usd + 1.0,
        )
        with pytest.raises(BudgetExceededError):
            await ego_session.run_cycle()
        mock_invoker.run.assert_not_called()

    async def test_run_cycle_cc_error(
        self, ego_session, mock_invoker, mock_session_manager,
    ):
        """CC error causes graceful failure."""
        mock_invoker.run.return_value = _cc_output(is_error=True)
        cycle = await ego_session.run_cycle()

        assert cycle is None
        mock_session_manager.fail.assert_called_once()

    async def test_run_cycle_cc_exception(
        self, ego_session, mock_invoker, mock_session_manager,
    ):
        """CC invocation exception causes graceful failure."""
        mock_invoker.run.side_effect = TimeoutError("CC timed out")
        cycle = await ego_session.run_cycle()

        assert cycle is None
        mock_session_manager.fail.assert_called_once()

    async def test_run_cycle_parse_failure(
        self, ego_session, mock_invoker, mock_proposal_workflow,
    ):
        """Invalid output stores cycle but creates no proposals."""
        mock_invoker.run.return_value = _cc_output("not json at all")
        cycle = await ego_session.run_cycle()

        assert cycle is not None
        assert cycle.proposals_json == "[]"
        mock_proposal_workflow.create_batch.assert_not_called()

    async def test_invocation_args(self, ego_session, mock_invoker):
        """Verify CCInvocation has correct model, effort, append mode."""
        await ego_session.run_cycle()

        invocation = mock_invoker.run.call_args[0][0]
        assert invocation.model.value == "opus"
        assert invocation.effort.value == "high"  # default from EgoConfig
        assert invocation.append_system_prompt is True
        assert invocation.skip_permissions is True

    async def test_context_assembled_before_cycle(
        self, ego_session, mock_compaction, mock_invoker,
    ):
        """assemble_context is called to build operational context."""
        await ego_session.run_cycle()
        mock_compaction.assemble_context.assert_called_once()

    async def test_session_manager_lifecycle(
        self, ego_session, mock_session_manager,
    ):
        """Session is created and completed on success."""
        await ego_session.run_cycle()
        mock_session_manager.create_background.assert_called_once()
        mock_session_manager.complete.assert_called_once()

    async def test_proposal_batch_created(
        self, ego_session, mock_proposal_workflow,
    ):
        """Proposals from ego output are sent as a batch."""
        await ego_session.run_cycle()
        mock_proposal_workflow.create_batch.assert_called_once()
        mock_proposal_workflow.send_digest.assert_called_once()

    async def test_ephemeral_no_resume(self, ego_session, mock_invoker):
        """Every cycle is ephemeral — resume_session_id is always None."""
        await ego_session.run_cycle()
        invocation = mock_invoker.run.call_args[0][0]
        assert invocation.resume_session_id is None

    async def test_ephemeral_consecutive_cycles(self, ego_session, mock_invoker):
        """Consecutive cycles are independent — no resume between them."""
        await ego_session.run_cycle()
        mock_invoker.run.reset_mock()

        await ego_session.run_cycle()
        invocation = mock_invoker.run.call_args[0][0]
        assert invocation.resume_session_id is None

    async def test_morning_report_uses_sonnet_low(self, ego_session, mock_invoker):
        """Morning report cycle uses Sonnet/Low per cycle type defaults."""
        await ego_session.run_cycle(is_morning_report=True)
        invocation = mock_invoker.run.call_args[0][0]
        assert invocation.model.value == "sonnet"
        assert invocation.effort.value == "low"

    async def test_cycle_type_proactive(self, ego_session, mock_invoker):
        """Proactive cycle type uses Opus/High."""
        await ego_session.run_cycle(cycle_type=CycleType.PROACTIVE)
        invocation = mock_invoker.run.call_args[0][0]
        assert invocation.model.value == "opus"
        assert invocation.effort.value == "high"

    async def test_cycle_type_escalation(self, ego_session, mock_invoker):
        """Escalation cycle type uses Sonnet/Medium."""
        await ego_session.run_cycle(cycle_type=CycleType.ESCALATION)
        invocation = mock_invoker.run.call_args[0][0]
        assert invocation.model.value == "sonnet"
        assert invocation.effort.value == "medium"

    async def test_system_prompt_is_static(self, ego_session, mock_invoker):
        """System prompt is the static identity — no dynamic content."""
        await ego_session.run_cycle()
        invocation = mock_invoker.run.call_args[0][0]
        # System prompt should be the cached static prompt
        assert invocation.system_prompt == ego_session._static_prompt

    async def test_dynamic_context_in_user_message(self, ego_session, mock_invoker):
        """Operational context appears in the user message, not system prompt."""
        await ego_session.run_cycle()
        invocation = mock_invoker.run.call_args[0][0]
        assert "Test operational context" in invocation.prompt


# ---------------------------------------------------------------------------
# Output parsing tests
# ---------------------------------------------------------------------------


class TestOutputParsing:
    def test_direct_json(self):
        raw = _valid_output()
        result = EgoSession._parse_output(raw)
        assert result is not None
        assert len(result["proposals"]) == 1
        assert result["focus_summary"] == "investigating backlog growth"

    def test_markdown_wrapped(self):
        raw = "Here's my analysis:\n```json\n" + _valid_output() + "\n```\n"
        result = EgoSession._parse_output(raw)
        assert result is not None
        assert result["focus_summary"] == "investigating backlog growth"

    def test_brace_extraction(self):
        raw = "Let me think about this...\n" + _valid_output() + "\nDone."
        result = EgoSession._parse_output(raw)
        assert result is not None
        assert result["focus_summary"] == "investigating backlog growth"

    def test_missing_required_field(self):
        raw = json.dumps({"proposals": [], "focus_summary": "test"})
        result = EgoSession._parse_output(raw)
        assert result is None  # missing follow_ups

    def test_garbage_input(self):
        assert EgoSession._parse_output("hello world") is None

    def test_empty_input(self):
        assert EgoSession._parse_output("") is None
        assert EgoSession._parse_output("   ") is None

    def test_proposals_not_list(self):
        raw = json.dumps({
            "proposals": "not a list",
            "focus_summary": "test",
            "follow_ups": [],
        })
        assert EgoSession._parse_output(raw) is None

    def test_valid_empty_proposals(self):
        raw = _valid_output(proposals=[], follow_ups=[])
        result = EgoSession._parse_output(raw)
        assert result is not None
        assert result["proposals"] == []
