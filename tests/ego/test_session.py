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
from genesis.ego.session import EgoSession, _sanitize_focus_summary
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
    communication_decision: str = "send_digest",
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
        "communication_decision": communication_decision,
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
    return EgoConfig(ego_thinking_budget_usd=10.0, ego_dispatch_budget_usd=5.0)


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
            cost_usd=config.ego_thinking_budget_usd + 1.0,
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
# Execution brief tests
# ---------------------------------------------------------------------------


class TestProcessExecutionBriefs:
    """Tests for _process_execution_briefs — the ego-as-executor path."""

    @pytest.fixture
    def mock_direct_runner(self):
        runner = AsyncMock()
        runner.spawn.return_value = "dispatched_sess_1"
        return runner

    @pytest.fixture
    def ego_with_runner(
        self, mock_invoker, mock_session_manager, mock_compaction,
        mock_context_builder, mock_proposal_workflow, dispatcher,
        config, db, mock_direct_runner,
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
            direct_session_runner=mock_direct_runner,
        )

    async def _insert_proposal(self, db, proposal_id, status="approved"):
        """Insert a proposal into the DB for testing."""
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, action_category, content, rationale, "
            "confidence, urgency, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (proposal_id, "investigate", "test", "test content",
             "test rationale", 0.8, "normal", status),
        )
        await db.commit()

    async def test_happy_path(self, ego_with_runner, mock_direct_runner, db):
        """Approved proposal dispatches via DirectSessionRunner."""
        await self._insert_proposal(db, "prop_001")

        briefs = [{"proposal_id": "prop_001", "prompt": "Do the thing"}]
        await ego_with_runner._process_execution_briefs(briefs)

        mock_direct_runner.spawn.assert_called_once()
        req = mock_direct_runner.spawn.call_args[0][0]
        assert req.prompt == "Do the thing"
        assert req.source_tag == "ego_dispatch"
        assert req.caller_context == "ego_proposal:prop_001"

        # Proposal should be transitioned to executed
        prop = await ego_crud.get_proposal(db, "prop_001")
        assert prop["status"] == "executed"

    async def test_pending_proposal_skipped(self, ego_with_runner, mock_direct_runner, db):
        """Non-approved proposal in execution brief is skipped."""
        await self._insert_proposal(db, "prop_002", status="pending")

        briefs = [{"proposal_id": "prop_002", "prompt": "Do the thing"}]
        await ego_with_runner._process_execution_briefs(briefs)

        mock_direct_runner.spawn.assert_not_called()
        # Proposal stays pending
        prop = await ego_crud.get_proposal(db, "prop_002")
        assert prop["status"] == "pending"

    async def test_rejected_proposal_skipped(self, ego_with_runner, mock_direct_runner, db):
        """Rejected proposal in execution brief is skipped."""
        await self._insert_proposal(db, "prop_003", status="rejected")

        briefs = [{"proposal_id": "prop_003", "prompt": "Do the thing"}]
        await ego_with_runner._process_execution_briefs(briefs)

        mock_direct_runner.spawn.assert_not_called()

    async def test_nonexistent_proposal_skipped(self, ego_with_runner, mock_direct_runner, db):
        """Execution brief for unknown proposal ID is skipped."""
        briefs = [{"proposal_id": "does_not_exist", "prompt": "Do the thing"}]
        await ego_with_runner._process_execution_briefs(briefs)

        mock_direct_runner.spawn.assert_not_called()

    async def test_spawn_failure_marks_failed(self, ego_with_runner, mock_direct_runner, db):
        """DirectSessionRunner failure transitions proposal to failed."""
        await self._insert_proposal(db, "prop_004")
        mock_direct_runner.spawn.side_effect = RuntimeError("spawn failed")

        briefs = [{"proposal_id": "prop_004", "prompt": "Do the thing"}]
        await ego_with_runner._process_execution_briefs(briefs)

        prop = await ego_crud.get_proposal(db, "prop_004")
        assert prop["status"] == "failed"

    async def test_no_runner_returns_early(self, ego_session, db):
        """No DirectSessionRunner → log warning and return."""
        await self._insert_proposal(db, "prop_005")

        briefs = [{"proposal_id": "prop_005", "prompt": "Do the thing"}]
        await ego_session._process_execution_briefs(briefs)

        # Proposal unchanged (no runner to dispatch)
        prop = await ego_crud.get_proposal(db, "prop_005")
        assert prop["status"] == "approved"

    async def test_dispatch_budget_exceeded(self, ego_with_runner, mock_direct_runner, db, config):
        """Dispatch budget exhausted → no spawns."""
        config.ego_dispatch_budget_usd = 0.0  # Exhausted
        await self._insert_proposal(db, "prop_006")

        briefs = [{"proposal_id": "prop_006", "prompt": "Do the thing"}]
        await ego_with_runner._process_execution_briefs(briefs)

        mock_direct_runner.spawn.assert_not_called()
        # Proposal unchanged
        prop = await ego_crud.get_proposal(db, "prop_006")
        assert prop["status"] == "approved"

    async def test_profile_mapping(self, ego_with_runner, mock_direct_runner, db):
        """Profile and model from brief are passed to the request."""
        await self._insert_proposal(db, "prop_007")

        briefs = [{"proposal_id": "prop_007", "prompt": "Research this",
                    "profile": "research", "model": "haiku"}]
        await ego_with_runner._process_execution_briefs(briefs)

        req = mock_direct_runner.spawn.call_args[0][0]
        assert req.profile == "research"
        assert req.model.value == "haiku"

    async def test_invalid_profile_defaults_observe(self, ego_with_runner, mock_direct_runner, db):
        """Invalid profile falls back to observe."""
        await self._insert_proposal(db, "prop_008")

        briefs = [{"proposal_id": "prop_008", "prompt": "Do it",
                    "profile": "admin"}]
        await ego_with_runner._process_execution_briefs(briefs)

        req = mock_direct_runner.spawn.call_args[0][0]
        assert req.profile == "observe"

    async def test_interact_profile_accepted(self, ego_with_runner, mock_direct_runner, db):
        """Interact profile is valid and passes through to the request."""
        await self._insert_proposal(db, "prop_009")

        briefs = [{"proposal_id": "prop_009", "prompt": "Publish a Medium post",
                    "profile": "interact", "model": "sonnet"}]
        await ego_with_runner._process_execution_briefs(briefs)

        req = mock_direct_runner.spawn.call_args[0][0]
        assert req.profile == "interact"

    async def test_empty_brief_skipped(self, ego_with_runner, mock_direct_runner, db):
        """Brief missing proposal_id or prompt is silently skipped."""
        briefs = [
            {"proposal_id": "", "prompt": "Do it"},     # empty ID
            {"proposal_id": "x", "prompt": ""},          # empty prompt
            {"not_a_real": "brief"},                      # wrong keys
        ]
        await ego_with_runner._process_execution_briefs(briefs)

        mock_direct_runner.spawn.assert_not_called()


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


# ---------------------------------------------------------------------------
# Focus summary sanitization tests
# ---------------------------------------------------------------------------


class TestFocusSanitization:
    """Tests for behavioral focus_summary detection and sanitization."""

    @pytest.mark.parametrize("focus", [
        "Holding back — Jay is sprinting on job applications",
        "Holding back — user is busy",
        "Stepping back while user is busy",
        "Lying low during sprint",
        "Waiting for user to surface",
        "Waiting for Jay to engage with proposals",
        "Waiting for them to return",
        "Pausing proactive work until things settle",
        "Pausing proposal generation",
        "Quiet mode — no proposals for now",
        "No proposals until he finishes applications",
        "No proposal for now",
        "Staying quiet while user focuses",
        "Staying out of the way",
        "Observing only — reduced activity",
        "Observing quietly this cycle",
        "Passive mode — watching only",
        "Minimal engagement this cycle",
        "Reduced activity during user sprint",
        "Not proposing anything while user is busy",
        "Not acting until signals improve",
        "Backing off — proposals ignored",
        "Until Jay surfaces again",
        "Letting things breathe for now",
        "Giving the user space this cycle",
        "Hibernating until user returns",
    ])
    def test_behavioral_focus_rejected(self, focus):
        sanitized, violated = _sanitize_focus_summary(focus)
        assert violated is True
        assert sanitized == "general system awareness"

    @pytest.mark.parametrize("focus", [
        "investigating backlog growth",
        "monitoring provider health after outage",
        "evaluating job application tracking design",
        "reviewing cost trends for the past week",
        "general system health monitoring",
        "analyzing user feedback on morning reports",
        "tracking Anthropic API availability",
        "memory pipeline performance audit",
        "waiting for API rate limit to reset",
        "observing provider latency patterns across regions",
    ])
    def test_legitimate_focus_accepted(self, focus):
        sanitized, violated = _sanitize_focus_summary(focus)
        assert violated is False
        assert sanitized == focus

    def test_previous_focus_used_as_fallback(self):
        sanitized, violated = _sanitize_focus_summary(
            "Holding back", previous_focus="monitoring API costs"
        )
        assert violated is True
        assert sanitized == "monitoring API costs"

    def test_default_fallback_when_no_previous(self):
        sanitized, violated = _sanitize_focus_summary("Holding back")
        assert violated is True
        assert sanitized == "general system awareness"

    def test_validate_output_sanitizes_focus(self):
        """_validate_output catches behavioral focus and sets violation flags."""
        raw = _valid_output(focus="Holding back — user is busy")
        result = EgoSession._parse_output(raw)
        assert result is not None
        assert result["focus_summary"] == "general system awareness"
        assert result.get("_focus_violation") is True
        assert "Holding back" in result.get("_original_focus", "")
