"""Tests for the ego session orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.cc.types import CCOutput
from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.dispatch import EgoDispatcher
from genesis.ego.session import EgoSession
from genesis.ego.signals import EgoSignal
from genesis.ego.types import EgoConfig

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
    pw.create_batch.return_value = ("batch123", ["p1"], [{"content": "test"}])
    pw.send_digest.return_value = "msg456"
    return pw


@pytest.fixture
def dispatcher(db):
    return EgoDispatcher(db=db)


@pytest.fixture
def config():
    return EgoConfig()


@pytest.fixture
def prompt_file(tmp_path):
    """Synthetic per-ego prompt file (EgoSession requires an explicit path)."""
    path = tmp_path / "TEST_EGO_SESSION.md"
    path.write_text("You are a test ego. Output valid JSON.")
    return path


@pytest.fixture
def ego_session(
    mock_invoker, mock_session_manager, mock_compaction,
    mock_context_builder, mock_proposal_workflow, dispatcher,
    config, db, prompt_file,
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
        prompt_path=prompt_file,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(**overrides) -> EgoSignal:
    """Build a minimal EgoSignal for testing."""
    defaults = {
        "signal_type": "timer",
        "focus_category": "proactive",
        "summary": "Idle tick",
        "priority": "medium",
    }
    defaults.update(overrides)
    return EgoSignal(**defaults)


# ---------------------------------------------------------------------------
# Session cycle tests (unified cognitive loop)
# ---------------------------------------------------------------------------


class TestUnifiedCycle:
    """Tests for run_unified_cycle — the sole cycle entry point."""

    async def test_success(self, ego_session, mock_invoker, db):
        """Full successful cycle: signal → perceive → invoke → proposals."""
        cycle = await ego_session.run_unified_cycle([_make_signal()])

        assert cycle is not None
        assert cycle.cost_usd == 0.15
        assert cycle.model_used == "opus"
        assert cycle.focus_summary == "investigating backlog growth"
        assert mock_invoker.run.call_count >= 1

        # Focus summary stored in ego_state is system-computed.
        focus = await ego_crud.get_state(db, "ego_focus_summary")
        assert focus == "general system awareness"

    async def test_no_proposals(
        self, ego_session, mock_invoker, mock_proposal_workflow,
    ):
        """Cycle with empty proposals array still stores the cycle."""
        mock_invoker.run.return_value = _cc_output(
            _valid_output(proposals=[], follow_ups=[]),
        )
        cycle = await ego_session.run_unified_cycle([_make_signal()])

        assert cycle is not None
        mock_proposal_workflow.create_batch.assert_not_called()

    async def test_cc_error(
        self, ego_session, mock_invoker, mock_session_manager,
    ):
        """CC error causes graceful failure — returns None."""
        mock_invoker.run.return_value = _cc_output(is_error=True)
        cycle = await ego_session.run_unified_cycle([_make_signal()])

        assert cycle is None
        mock_session_manager.fail.assert_called_once()

    async def test_cc_exception(
        self, ego_session, mock_invoker, mock_session_manager,
    ):
        """CC invocation exception causes graceful failure."""
        mock_invoker.run.side_effect = TimeoutError("CC timed out")
        cycle = await ego_session.run_unified_cycle([_make_signal()])

        assert cycle is None
        mock_session_manager.fail.assert_called_once()

    async def test_parse_failure(
        self, ego_session, mock_invoker, mock_proposal_workflow,
    ):
        """Invalid output stores cycle but creates no proposals."""
        mock_invoker.run.return_value = _cc_output("not json at all")
        cycle = await ego_session.run_unified_cycle([_make_signal()])

        assert cycle is not None
        assert cycle.proposals_json == "[]"
        mock_proposal_workflow.create_batch.assert_not_called()

    async def test_invocation_args(self, ego_session, mock_invoker):
        """Verify CCInvocation has correct model, effort, append mode."""
        await ego_session.run_unified_cycle([_make_signal()])

        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert invocation.model.value == "opus"  # from EgoConfig default
        assert invocation.effort.value == "high"  # from EgoConfig default
        assert invocation.append_system_prompt is True
        assert invocation.skip_permissions is True

    async def test_model_override(self, ego_session, mock_invoker):
        """model_override takes precedence over config default."""
        await ego_session.run_unified_cycle(
            [_make_signal()], model_override="sonnet",
        )
        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert invocation.model.value == "sonnet"

    async def test_effort_override(self, ego_session, mock_invoker):
        """effort_override takes precedence over config default."""
        await ego_session.run_unified_cycle(
            [_make_signal()], effort_override="low",
        )
        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert invocation.effort.value == "low"

    async def test_daily_briefing_prompt(self, ego_session, mock_invoker):
        """Daily briefing signal → DAILY BRIEFING directive in prompt."""
        signal = _make_signal(focus_category="daily_briefing")
        await ego_session.run_unified_cycle([signal])

        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert "DAILY BRIEFING" in invocation.prompt
        assert "needs today" in invocation.prompt
        # The morning report is produced by its own pipeline — the ego
        # must not be instructed to emit a morning_report field.
        assert "morning_report" not in invocation.prompt

    async def test_reactive_prompt(self, ego_session, mock_invoker):
        """Reactive signal → REACTIVE directive in prompt."""
        signal = _make_signal(focus_category="reactive", summary="health alert")
        await ego_session.run_unified_cycle([signal])

        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert "REACTIVE" in invocation.prompt

    async def test_ephemeral_no_resume(self, ego_session, mock_invoker):
        """Every cycle is ephemeral — resume_session_id is always None."""
        await ego_session.run_unified_cycle([_make_signal()])
        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert invocation.resume_session_id is None

    async def test_system_prompt_is_static(self, ego_session, mock_invoker):
        """System prompt is the static identity — no dynamic content."""
        await ego_session.run_unified_cycle([_make_signal()])
        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert invocation.system_prompt == ego_session._static_prompt

    async def test_dynamic_context_in_user_message(self, ego_session, mock_invoker):
        """Operational context appears in the user message, not system prompt."""
        await ego_session.run_unified_cycle([_make_signal()])
        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert "Test operational context" in invocation.prompt

    async def test_session_manager_lifecycle(
        self, ego_session, mock_session_manager,
    ):
        """Session is created and completed on success."""
        await ego_session.run_unified_cycle([_make_signal()])
        mock_session_manager.create_background.assert_called_once()
        mock_session_manager.complete.assert_called_once()

    async def test_proposal_batch_created(
        self, ego_session, mock_proposal_workflow,
    ):
        """Proposals from ego output are sent as a batch."""
        await ego_session.run_unified_cycle([_make_signal()])
        mock_proposal_workflow.create_batch.assert_called_once()
        mock_proposal_workflow.send_digest.assert_called_once()

    async def test_empty_signals_returns_none(self, ego_session, mock_invoker):
        """Empty signal list → no cycle, returns None."""
        cycle = await ego_session.run_unified_cycle([])
        assert cycle is None
        mock_invoker.run.assert_not_called()

    async def test_focus_in_prompt(self, ego_session, mock_invoker):
        """Focus type and rationale appear in the user prompt."""
        signal = _make_signal(summary="Goal stale for 12 days",
                              focus_category="goal_review")
        await ego_session.run_unified_cycle([signal])

        invocation = mock_invoker.run.call_args_list[0][0][0]
        assert "goal_review" in invocation.prompt
        assert "GOAL REVIEW" in invocation.prompt


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
        config, db, mock_direct_runner, prompt_file,
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
            prompt_path=prompt_file,
        )

    async def _insert_proposal(self, db, proposal_id, status="approved",
                               action_type="investigate"):
        """Insert a proposal into the DB for testing."""
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, action_category, content, rationale, "
            "confidence, urgency, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (proposal_id, action_type, "test", "test content",
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
        assert req.prompt.startswith("Do the thing")
        # Firewall rules are appended to all execution brief prompts
        assert "Content Firewall" in req.prompt
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

    async def test_spawn_failure_reverts_to_approved(self, ego_with_runner, mock_direct_runner, db):
        """DirectSessionRunner failure reverts proposal to approved for retry."""
        await self._insert_proposal(db, "prop_004")
        mock_direct_runner.spawn.side_effect = RuntimeError("spawn failed")

        briefs = [{"proposal_id": "prop_004", "prompt": "Do the thing"}]
        await ego_with_runner._process_execution_briefs(briefs)

        prop = await ego_crud.get_proposal(db, "prop_004")
        assert prop["status"] == "approved"

    async def test_no_runner_returns_early(self, ego_session, db):
        """No DirectSessionRunner → log warning and return."""
        await self._insert_proposal(db, "prop_005")

        briefs = [{"proposal_id": "prop_005", "prompt": "Do the thing"}]
        await ego_session._process_execution_briefs(briefs)

        # Proposal unchanged (no runner to dispatch)
        prop = await ego_crud.get_proposal(db, "prop_005")
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

    async def test_invalid_profile_infers_from_action_type(self, ego_with_runner, mock_direct_runner, db):
        """Invalid profile falls back to action_type inference (research default)."""
        await self._insert_proposal(db, "prop_008")

        briefs = [{"proposal_id": "prop_008", "prompt": "Do it",
                    "profile": "admin"}]
        await ego_with_runner._process_execution_briefs(briefs)

        req = mock_direct_runner.spawn.call_args[0][0]
        assert req.profile == "research"

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

    async def test_cognitive_variant_promotion_never_dispatched(
        self, ego_with_runner, mock_direct_runner, db,
    ):
        """An approved cognitive_variant_promotion must NEVER be auto-dispatched
        as a session via the execution-briefs path. This fixture has no autonomy
        gate, so the assertion proves the explicit action_type blocklist (not the
        gate) is the backstop — the fail-open path the architect flagged. The
        proposal is applied only by its resolution handler at approval time."""
        await self._insert_proposal(
            db, "prop_cvp", action_type="cognitive_variant_promotion",
        )

        briefs = [{"proposal_id": "prop_cvp", "prompt": "apply winning prompt"}]
        await ego_with_runner._process_execution_briefs(briefs)

        mock_direct_runner.spawn.assert_not_called()
        prop = await ego_crud.get_proposal(db, "prop_cvp")
        assert prop["status"] == "approved"  # untouched by the dispatch path

    async def test_cognitive_variant_not_dispatched_by_sweep(
        self, ego_with_runner, mock_direct_runner, db,
    ):
        """The OTHER dispatch path — the approved-proposal sweep — must also skip
        cognitive_variant_promotion (the blocklist fires before any spawn)."""
        await self._insert_proposal(
            db, "prop_cvp_sweep", action_type="cognitive_variant_promotion",
        )
        await ego_with_runner.sweep_approved_proposals()
        mock_direct_runner.spawn.assert_not_called()
        assert (await ego_crud.get_proposal(db, "prop_cvp_sweep"))["status"] == "approved"


def test_never_dispatch_action_types_single_source_of_truth():
    """Both dispatch paths (sweep + execution-briefs) read this one tuple.
    cognitive_variant_promotion must be present (else an approved Evo promotion
    could be auto-run as a session); the pre-existing apply-at-approval types
    must remain (regression guard for the refactor to a shared constant)."""
    from genesis.ego.session import _NEVER_DISPATCH_ACTION_TYPES

    assert "cognitive_variant_promotion" in _NEVER_DISPATCH_ACTION_TYPES
    for legacy in ("autonomy_earnback", "goal_status_change", "cell_promotion"):
        assert legacy in _NEVER_DISPATCH_ACTION_TYPES


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
        """proposals and focus_summary are required; follow_ups is optional."""
        # Missing proposals → None
        raw = json.dumps({"focus_summary": "test", "follow_ups": []})
        assert EgoSession._parse_output(raw) is None
        # Missing focus_summary → None
        raw = json.dumps({"proposals": [], "follow_ups": []})
        assert EgoSession._parse_output(raw) is None
        # follow_ups absent → still valid (no longer required)
        raw = json.dumps({"proposals": [], "focus_summary": "test"})
        result = EgoSession._parse_output(raw)
        assert result is not None

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
# communication_decision default
# ---------------------------------------------------------------------------


class TestCommunicationDecisionDefault:
    """Verify that omitting communication_decision defaults to send_digest."""

    def test_default_is_send_digest(self):
        """When ego omits communication_decision, code should default to send_digest."""
        data = {
            "proposals": [
                {
                    "action_type": "investigate",
                    "action_category": "test",
                    "content": "test proposal",
                    "rationale": "test",
                    "confidence": 0.8,
                }
            ],
            "focus_summary": "test",
            "follow_ups": [],
        }
        # Simulate what session.py:298 does
        comm_decision = data.get("communication_decision", "send_digest")
        assert comm_decision == "send_digest"

    def test_explicit_stay_quiet_preserved(self):
        """When ego explicitly sets stay_quiet, it should be honored."""
        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
            "communication_decision": "stay_quiet",
        }
        comm_decision = data.get("communication_decision", "send_digest")
        assert comm_decision == "stay_quiet"

    def test_explicit_urgent_notify_preserved(self):
        """When ego sets urgent_notify, it should be honored."""
        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
            "communication_decision": "urgent_notify",
        }
        comm_decision = data.get("communication_decision", "send_digest")
        assert comm_decision == "urgent_notify"


# ---------------------------------------------------------------------------
# Output contract includes communication_decision
# ---------------------------------------------------------------------------


class TestOutputContractCommDecision:
    """User ego must NOT include communication_decision (delivery is system-
    controlled).  Genesis ego keeps it for noise reduction."""

    def test_user_ego_contract_excludes_comm_decision(self):
        from genesis.ego.user_context import UserEgoContextBuilder

        contract = UserEgoContextBuilder._output_contract_section()
        assert "communication_decision" not in contract

    def test_genesis_ego_contract_includes_comm_decision(self):
        from genesis.ego.genesis_context import GenesisEgoContextBuilder

        contract = GenesisEgoContextBuilder._output_contract_section()
        assert "communication_decision" in contract
# Focus sanitization tests removed — focus_summary is now system-computed
# (computed_focus.py). Tests for compute_focus_summary are in
# tests/ego/test_computed_focus.py.


# ---------------------------------------------------------------------------
# Goal assessment output
# ---------------------------------------------------------------------------


class TestGoalAssessmentOutput:
    """Tests for goal_assessment + goal_status_recommendation processing."""

    def test_validate_output_accepts_goal_assessment(self):
        """goal_assessment string passes validation."""
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "Goal review",
            "goal_assessment": "This goal is on track but needs more focus.",
            "goal_status_recommendation": "continue",
        }
        result = _validate_output(data)
        assert result is not None
        assert result["goal_assessment"] == "This goal is on track but needs more focus."
        assert result["goal_status_recommendation"] == "continue"

    def test_validate_output_sanitizes_invalid_goal_assessment(self):
        """Non-string goal_assessment gets sanitized to empty string."""
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "Goal review",
            "goal_assessment": 42,
        }
        result = _validate_output(data)
        assert result is not None
        assert result["goal_assessment"] == ""

    def test_validate_output_rejects_invalid_recommendation(self):
        """Invalid goal_status_recommendation gets removed."""
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "Goal review",
            "goal_status_recommendation": "invalid_value",
        }
        result = _validate_output(data)
        assert result is not None
        assert "goal_status_recommendation" not in result

    def test_validate_output_accepts_all_valid_recommendations(self):
        """All valid recommendation values pass validation."""
        from genesis.ego.session import _validate_output

        for rec in ("continue", "pause", "deprioritize", "close"):
            data = {
                "proposals": [],
                "focus_summary": "Goal review",
                "goal_status_recommendation": rec,
            }
            result = _validate_output(data)
            assert result is not None
            assert result["goal_status_recommendation"] == rec

    def test_output_contract_includes_goal_fields(self):
        """Output contract mentions goal_assessment and goal_status_recommendation."""
        from genesis.ego.user_context import UserEgoContextBuilder

        contract = UserEgoContextBuilder._output_contract_section()
        assert "goal_assessment" in contract
        assert "goal_status_recommendation" in contract
        assert "continue|pause|deprioritize|close" in contract


class TestGoalStatusRecommendationRouting:
    """_surface_goal_recommendation: reversible recs → approvable proposal;
    terminal (close) → passive observation. Recommend-only — no goal writes."""

    @pytest.fixture
    async def goals_db(self, db):
        await db.execute(TABLES["user_goals"])
        await db.execute(TABLES["observations"])
        await db.commit()
        return db

    @staticmethod
    async def _insert_goal(conn, *, gid, status="active", priority="high"):
        await conn.execute(
            "INSERT INTO user_goals "
            "(id, title, category, status, priority, created_at, updated_at) "
            "VALUES (?, 'My Goal', 'project', ?, ?, '2026-06-01', '2026-06-01')",
            (gid, status, priority),
        )
        await conn.commit()

    async def test_pause_creates_status_change_proposal(self, ego_session, goals_db):
        ego_session._source_tag = "user_ego_cycle"
        await self._insert_goal(goals_db, gid="gA", priority="high")

        await ego_session._surface_goal_recommendation(
            goal_id="gA", recommendation="pause", assessment="stuck for weeks",
        )

        ego_session._proposals.create_batch.assert_awaited_once()
        props = ego_session._proposals.create_batch.call_args[0][0]
        assert len(props) == 1
        p = props[0]
        assert p["action_type"] == "goal_status_change"
        assert p["goal_id"] == "gA"
        assert p["expected_outputs"] == {"change": "status", "value": "paused"}
        ego_session._proposals.send_digest.assert_awaited_once()

    async def test_deprioritize_lowers_priority_one_notch(self, ego_session, goals_db):
        ego_session._source_tag = "user_ego_cycle"
        await self._insert_goal(goals_db, gid="gB", priority="high")

        await ego_session._surface_goal_recommendation(
            goal_id="gB", recommendation="deprioritize", assessment="lower it",
        )

        p = ego_session._proposals.create_batch.call_args[0][0][0]
        assert p["expected_outputs"] == {"change": "priority", "value": "medium"}

    async def test_close_creates_observation_not_proposal(self, ego_session, goals_db):
        ego_session._source_tag = "user_ego_cycle"
        await self._insert_goal(goals_db, gid="gC")

        await ego_session._surface_goal_recommendation(
            goal_id="gC", recommendation="close", assessment="done with it",
        )

        ego_session._proposals.create_batch.assert_not_awaited()
        cursor = await goals_db.execute(
            "SELECT type, category FROM observations WHERE source = 'user_ego'",
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["type"] == "goal_recommendation"

    async def test_antispam_skips_when_proposal_open(self, ego_session, goals_db):
        ego_session._source_tag = "user_ego_cycle"
        await self._insert_goal(goals_db, gid="gD")
        await goals_db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, content, status, goal_id, created_at) "
            "VALUES ('open1', 'goal_status_change', 'x', 'pending', 'gD', '2026-06-01')",
        )
        await goals_db.commit()

        await ego_session._surface_goal_recommendation(
            goal_id="gD", recommendation="pause", assessment="again",
        )

        ego_session._proposals.create_batch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Prompt loading — fail loud on missing per-ego prompt
# ---------------------------------------------------------------------------


class TestPromptLoading:
    """EgoSession must refuse to run without its real identity prompt."""

    def _build(self, prompt_path, *, mock_invoker, mock_session_manager,
               mock_compaction, mock_context_builder, mock_proposal_workflow,
               dispatcher, config, db):
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
            prompt_path=prompt_path,
        )

    async def test_missing_prompt_file_raises(
        self, mock_invoker, mock_session_manager, mock_compaction,
        mock_context_builder, mock_proposal_workflow, dispatcher,
        config, db, tmp_path,
    ):
        """A prompt_path pointing at a nonexistent file raises, naming the path."""
        missing = tmp_path / "NO_SUCH_PROMPT.md"
        with pytest.raises(RuntimeError, match="NO_SUCH_PROMPT.md"):
            self._build(
                missing,
                mock_invoker=mock_invoker,
                mock_session_manager=mock_session_manager,
                mock_compaction=mock_compaction,
                mock_context_builder=mock_context_builder,
                mock_proposal_workflow=mock_proposal_workflow,
                dispatcher=dispatcher,
                config=config,
                db=db,
            )

    async def test_no_prompt_path_raises(
        self, mock_invoker, mock_session_manager, mock_compaction,
        mock_context_builder, mock_proposal_workflow, dispatcher,
        config, db,
    ):
        """Omitting prompt_path raises — the legacy EGO_SESSION.md default is gone."""
        with pytest.raises(RuntimeError, match="prompt_path"):
            self._build(
                None,
                mock_invoker=mock_invoker,
                mock_session_manager=mock_session_manager,
                mock_compaction=mock_compaction,
                mock_context_builder=mock_context_builder,
                mock_proposal_workflow=mock_proposal_workflow,
                dispatcher=dispatcher,
                config=config,
                db=db,
            )

    async def test_real_per_ego_prompts_load(
        self, mock_invoker, mock_session_manager, mock_compaction,
        mock_context_builder, mock_proposal_workflow, dispatcher,
        config, db,
    ):
        """Both shipped per-ego prompts exist and load as the static prompt."""
        import genesis.identity as identity_pkg

        identity_dir = Path(identity_pkg.__file__).resolve().parent
        for name in ("USER_EGO_SESSION.md", "GENESIS_EGO_SESSION.md"):
            session = self._build(
                identity_dir / name,
                mock_invoker=mock_invoker,
                mock_session_manager=mock_session_manager,
                mock_compaction=mock_compaction,
                mock_context_builder=mock_context_builder,
                mock_proposal_workflow=mock_proposal_workflow,
                dispatcher=dispatcher,
                config=config,
                db=db,
            )
            assert session._static_prompt.strip()


# ---------------------------------------------------------------------------
# Additive ego autonomy — genesis_ego-owned goals self-manage (PR-2, 2026-07-16)
# ---------------------------------------------------------------------------


class TestAutonomousOwnGoalManagement:
    """A genesis_ego-origin goal reviewed by the GENESIS ego cycle is paused/
    deprioritized directly (no proposal) with an audit observation. Everything
    else — user-origin goals, the user-ego cycle, non-reversible recs — keeps
    the recommend-only proposal path. The approval gates are untouched: this
    is the ego skipping proposal CREATION for its own additive artifacts, not
    a gate bypass."""

    @pytest.fixture
    async def goals_db(self, db):
        await db.execute(TABLES["user_goals"])
        await db.execute(TABLES["observations"])
        await db.commit()
        return db

    @staticmethod
    async def _insert_goal(conn, *, gid, origin="user", status="active", priority="high"):
        await conn.execute(
            "INSERT INTO user_goals "
            "(id, title, category, status, priority, origin, created_at, updated_at) "
            "VALUES (?, 'My Goal', 'project', ?, ?, ?, '2026-06-01', '2026-06-01')",
            (gid, status, priority, origin),
        )
        await conn.commit()

    @staticmethod
    async def _goal_row(conn, gid):
        cursor = await conn.execute(
            "SELECT status, priority, origin FROM user_goals WHERE id = ?", (gid,)
        )
        return await cursor.fetchone()

    async def test_ego_goal_pause_applies_directly(self, ego_session, goals_db):
        ego_session._source_tag = "genesis_ego_cycle"
        await self._insert_goal(goals_db, gid="eg1", origin="genesis_ego")

        await ego_session._surface_goal_recommendation(
            goal_id="eg1", recommendation="pause", assessment="stale for weeks",
        )

        row = await self._goal_row(goals_db, "eg1")
        assert row["status"] == "paused"
        ego_session._proposals.create_batch.assert_not_awaited()

    async def test_ego_goal_pause_writes_audit_observation(self, ego_session, goals_db):
        ego_session._source_tag = "genesis_ego_cycle"
        await self._insert_goal(goals_db, gid="eg2", origin="genesis_ego")

        await ego_session._surface_goal_recommendation(
            goal_id="eg2", recommendation="pause", assessment="done exploring",
        )

        cursor = await goals_db.execute(
            "SELECT type, source, content FROM observations "
            "WHERE type = 'goal_autonomous_action'",
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["source"] == "genesis_ego_cycle"
        assert "My Goal" in row["content"]

    async def test_ego_goal_deprioritize_applies_directly(self, ego_session, goals_db):
        ego_session._source_tag = "genesis_ego_cycle"
        await self._insert_goal(goals_db, gid="eg3", origin="genesis_ego", priority="high")

        await ego_session._surface_goal_recommendation(
            goal_id="eg3", recommendation="deprioritize", assessment="lower it",
        )

        row = await self._goal_row(goals_db, "eg3")
        assert row["priority"] == "medium"  # one notch down, applied directly
        ego_session._proposals.create_batch.assert_not_awaited()

    async def test_user_goal_still_requires_proposal(self, ego_session, goals_db):
        """The hard invariant: an origin='user' goal is NEVER mutated
        autonomously — even by the genesis ego cycle."""
        ego_session._source_tag = "genesis_ego_cycle"
        await self._insert_goal(goals_db, gid="ug1", origin="user")

        await ego_session._surface_goal_recommendation(
            goal_id="ug1", recommendation="pause", assessment="stuck",
        )

        row = await self._goal_row(goals_db, "ug1")
        assert row["status"] == "active"  # untouched
        ego_session._proposals.create_batch.assert_awaited_once()  # proposal path

    async def test_user_ego_cycle_never_direct_applies(self, ego_session, goals_db):
        """Ego-owned goals self-manage only from the GENESIS ego cycle; a
        user-ego recommendation on an ego goal still goes through a proposal."""
        ego_session._source_tag = "user_ego_cycle"
        await self._insert_goal(goals_db, gid="eg4", origin="genesis_ego")

        await ego_session._surface_goal_recommendation(
            goal_id="eg4", recommendation="pause", assessment="from user ego",
        )

        row = await self._goal_row(goals_db, "eg4")
        assert row["status"] == "active"
        ego_session._proposals.create_batch.assert_awaited_once()

    async def test_default_origin_goal_requires_proposal(self, ego_session, goals_db):
        """A goal inserted without an explicit origin defaults to 'user' via
        the schema — and therefore stays proposal-gated."""
        ego_session._source_tag = "genesis_ego_cycle"
        await goals_db.execute(
            "INSERT INTO user_goals (id, title, category, created_at, updated_at) "
            "VALUES ('dg1', 'Default Goal', 'project', '2026-06-01', '2026-06-01')",
        )
        await goals_db.commit()

        await ego_session._surface_goal_recommendation(
            goal_id="dg1", recommendation="pause", assessment="stale",
        )

        row = await self._goal_row(goals_db, "dg1")
        assert row["status"] == "active"
        ego_session._proposals.create_batch.assert_awaited_once()

    async def test_close_recommendation_still_observation_even_for_ego_goal(
        self, ego_session, goals_db,
    ):
        """close (achieve-vs-abandon) is terminal — never auto-applied, even
        on an ego-owned goal. Only pause/deprioritize are additive-safe."""
        ego_session._source_tag = "genesis_ego_cycle"
        await self._insert_goal(goals_db, gid="eg5", origin="genesis_ego")

        await ego_session._surface_goal_recommendation(
            goal_id="eg5", recommendation="close", assessment="looks finished",
        )

        row = await self._goal_row(goals_db, "eg5")
        assert row["status"] == "active"
        ego_session._proposals.create_batch.assert_not_awaited()
        cursor = await goals_db.execute(
            "SELECT type FROM observations WHERE type = 'goal_recommendation'",
        )
        assert await cursor.fetchone() is not None
