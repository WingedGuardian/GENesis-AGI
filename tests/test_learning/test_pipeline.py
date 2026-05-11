"""Tests for the triage pipeline factory."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db import schema
from genesis.learning.pipeline import build_triage_pipeline
from genesis.learning.types import (
    DeltaClassification,
    DiscoveryAttribution,
    OutcomeClass,
    RequestDeliveryDelta,
    ScopeEvolution,
    TriageDepth,
    TriageResult,
)


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for ddl in schema.TABLES.values():
            await conn.execute(ddl)
        await conn.commit()
        yield conn


@dataclass
class FakeCCOutput:
    session_id: str = "sess-1"
    text: str = "Here is a long response with enough content to pass the filter"
    model_used: str = "test"
    cost_usd: float = 0.01
    input_tokens: int = 200
    output_tokens: int = 300
    duration_ms: int = 1000
    exit_code: int = 0
    is_error: bool = False
    error_message: str | None = None
    model_requested: str = ""
    downgraded: bool = False


@dataclass
class FakeRoutingResult:
    success: bool = True
    content: str = ""


def _make_triage_classifier(depth: TriageDepth = TriageDepth.SKIP):
    tc = MagicMock()
    tc.classify = AsyncMock(
        return_value=TriageResult(depth=depth, rationale="test", skipped_by_prefilter=False)
    )
    return tc


def _make_outcome_classifier(outcome: OutcomeClass = OutcomeClass.SUCCESS):
    oc = MagicMock()
    oc.classify = AsyncMock(return_value=outcome)
    return oc


def _make_delta_assessor():
    da = MagicMock()
    da.assess = AsyncMock(
        return_value=RequestDeliveryDelta(
            classification=DeltaClassification.EXACT_MATCH,
            scope_evolution=ScopeEvolution(
                original_request="test",
                final_delivery="test",
                scope_communicated=True,
            ),
            attributions=[DiscoveryAttribution.USER_REVISED_SCOPE],
            evidence="matched",
        )
    )
    return da


class TestTriagePipeline:
    @pytest.mark.asyncio
    async def test_skips_trivial_interaction(self, db):
        """Pipeline returns early for short interactions with no tools."""
        tc = _make_triage_classifier()
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=tc,
            outcome_classifier=_make_outcome_classifier(),
            delta_assessor=_make_delta_assessor(),
            observation_writer=MagicMock(),
        )
        output = FakeCCOutput(input_tokens=10, output_tokens=10)
        await pipeline(output, "hi", "terminal")
        tc.classify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_at_skip_depth(self, db):
        """Pipeline stops after classifier returns SKIP."""
        oc = _make_outcome_classifier()
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.SKIP),
            outcome_classifier=oc,
            delta_assessor=_make_delta_assessor(),
            observation_writer=MagicMock(),
        )
        await pipeline(FakeCCOutput(), "test query", "terminal")
        oc.classify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_classification_at_worth_thinking(self, db):
        """Pipeline runs outcome + delta at depth >= WORTH_THINKING."""
        oc = _make_outcome_classifier()
        da = _make_delta_assessor()
        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.WORTH_THINKING),
            outcome_classifier=oc,
            delta_assessor=da,
            observation_writer=ow,
        )
        await pipeline(FakeCCOutput(), "test query", "terminal")
        oc.classify.assert_awaited_once()
        da.assess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_classification_failed_skips_downstream(self, db):
        """When the classifier returns CLASSIFICATION_FAILED, the pipeline must
        skip delta assessment, attribution routing, autonomy adaptation, and
        procedure extraction — but still run debrief parsing (which is
        classification-independent).
        """
        oc = _make_outcome_classifier(OutcomeClass.CLASSIFICATION_FAILED)
        da = _make_delta_assessor()
        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        router = MagicMock()
        router.route_call = AsyncMock()
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=oc,
            delta_assessor=da,
            observation_writer=ow,
            router=router,
        )
        text_with_learnings = (
            "response\n## Learnings\n- something\n- else"
        )
        await pipeline(FakeCCOutput(text=text_with_learnings), "q", "terminal")

        oc.classify.assert_awaited_once()
        # Delta assessment must NOT fire — classifier failed
        da.assess.assert_not_awaited()
        # Procedure extraction routes through router — must NOT fire
        router.route_call.assert_not_awaited()
        # Debrief parsing is independent — should still write learnings
        learning_calls = [
            c for c in ow.write.call_args_list if c[1].get("source") == "cc_debrief"
        ]
        assert len(learning_calls) == 2

    @pytest.mark.asyncio
    async def test_writes_observation_at_full_analysis(self, db):
        """Pipeline writes observation at depth >= FULL_ANALYSIS."""
        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
        )
        await pipeline(FakeCCOutput(), "test query", "terminal")
        # Should have at least one write (the observation)
        assert ow.write.await_count >= 1

    @pytest.mark.asyncio
    async def test_parses_debrief_learnings(self, db):
        """Pipeline extracts learnings from output text."""
        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        text_with_learnings = (
            "Some response\n## Learnings\n- Always check the schema first\n- Use batch queries"
        )
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.QUICK_NOTE),
            outcome_classifier=_make_outcome_classifier(),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
        )
        output = FakeCCOutput(text=text_with_learnings)
        await pipeline(output, "test", "terminal")
        # Should write 2 learnings
        learning_calls = [
            c for c in ow.write.call_args_list if c[1].get("source") == "cc_debrief"
        ]
        assert len(learning_calls) == 2

    @pytest.mark.asyncio
    async def test_emits_triage_event(self, db):
        """Pipeline emits triage.classified event."""
        event_bus = AsyncMock()
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.QUICK_NOTE),
            outcome_classifier=_make_outcome_classifier(),
            delta_assessor=_make_delta_assessor(),
            observation_writer=MagicMock(write=AsyncMock(return_value="x")),
            event_bus=event_bus,
        )
        await pipeline(FakeCCOutput(), "test", "terminal")
        event_bus.emit.assert_awaited()
        call_kwargs = event_bus.emit.call_args[1]
        assert call_kwargs["event_type"] == "triage.classified"


class TestAutonomyCalibration:
    """Pipeline wires outcome classification to autonomy manager."""

    @pytest.mark.asyncio
    async def test_success_outcome_records_autonomy_success(self, db):
        """SUCCESS outcome triggers autonomy_manager.record_success."""
        mgr = AsyncMock()
        mgr.record_success = AsyncMock(return_value=(True, False))
        runtime = MagicMock()
        runtime._autonomy_manager = mgr
        runtime.record_job_success = MagicMock()
        runtime.record_job_failure = MagicMock()

        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.SUCCESS),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
            runtime=runtime,
        )
        await pipeline(FakeCCOutput(), "test", "terminal")
        mgr.record_success.assert_awaited_once_with("direct_session")

    @pytest.mark.asyncio
    async def test_failure_outcome_records_autonomy_correction(self, db):
        """APPROACH_FAILURE outcome triggers autonomy_manager.record_correction."""
        mgr = AsyncMock()
        mgr.record_correction = AsyncMock(return_value=(True, True))
        runtime = MagicMock()
        runtime._autonomy_manager = mgr
        runtime.record_job_success = MagicMock()
        runtime.record_job_failure = MagicMock()

        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.APPROACH_FAILURE),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
            runtime=runtime,
        )
        await pipeline(FakeCCOutput(), "test", "terminal")
        mgr.record_correction.assert_awaited_once()
        # Verify category is direct_session
        call_args = mgr.record_correction.call_args
        assert call_args[0][0] == "direct_session"

    @pytest.mark.asyncio
    async def test_no_autonomy_manager_does_not_crash(self, db):
        """Pipeline doesn't crash when runtime has no autonomy manager."""
        runtime = MagicMock()
        runtime._autonomy_manager = None
        runtime.record_job_success = MagicMock()
        runtime.record_job_failure = MagicMock()

        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.SUCCESS),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
            runtime=runtime,
        )
        # Should not raise
        await pipeline(FakeCCOutput(), "test", "terminal")


class TestSteeringRuleExtraction:
    """Steering rule extraction respects channel boundaries."""

    @pytest.mark.asyncio
    async def test_inbox_channel_does_not_write_steering(self, db):
        """Inbox evaluations must never write to STEERING.md."""
        loader = MagicMock()
        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.APPROACH_FAILURE),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
            identity_loader=loader,
        )
        await pipeline(FakeCCOutput(), "never do this wrong thing", "inbox")
        loader.add_steering_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_mail_channel_does_not_write_steering(self, db):
        """Mail evaluations must never write to STEERING.md."""
        loader = MagicMock()
        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.APPROACH_FAILURE),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
            identity_loader=loader,
        )
        await pipeline(FakeCCOutput(), "stop doing this wrong thing", "mail")
        loader.add_steering_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminal_channel_does_write_steering(self, db):
        """Foreground terminal sessions should extract steering rules."""
        loader = MagicMock()
        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.APPROACH_FAILURE),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
            identity_loader=loader,
        )
        await pipeline(FakeCCOutput(), "never do that again", "terminal")
        loader.add_steering_rule.assert_called_once()

    @pytest.mark.asyncio
    async def test_telegram_channel_does_write_steering(self, db):
        """Foreground Telegram sessions should extract steering rules."""
        loader = MagicMock()
        ow = MagicMock()
        ow.write = AsyncMock(return_value="obs-1")
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.APPROACH_FAILURE),
            delta_assessor=_make_delta_assessor(),
            observation_writer=ow,
            identity_loader=loader,
        )
        await pipeline(FakeCCOutput(), "don't ever do that", "telegram")
        loader.add_steering_rule.assert_called_once()


class TestSuccessExtractionChannelGate:
    """SUCCESS-path procedure extraction must only fire on autonomous channels.

    Foreground sessions store procedures opportunistically via the
    `procedure_store` MCP — they should NOT trigger auto-extraction on every
    successful task, which would flood the table.
    """

    @pytest.mark.asyncio
    async def test_success_on_autonomous_channel_triggers_extraction(self, db, monkeypatch):
        """SUCCESS on 'surplus' (autonomous) should call extract_procedure."""
        called = {"n": 0}

        async def fake_extract(*_args, **_kwargs):
            called["n"] += 1
            return None

        monkeypatch.setattr(
            "genesis.learning.pipeline.extract_procedure", fake_extract,
        )
        router = MagicMock()
        router.route_call = AsyncMock()
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.SUCCESS),
            delta_assessor=_make_delta_assessor(),
            observation_writer=MagicMock(write=AsyncMock(return_value="o")),
            router=router,
        )
        await pipeline(FakeCCOutput(), "q", "surplus")
        assert called["n"] == 1

    @pytest.mark.asyncio
    async def test_success_on_foreground_channel_skips_extraction(self, db, monkeypatch):
        """SUCCESS on 'terminal' (foreground) must NOT call extract_procedure."""
        called = {"n": 0}

        async def fake_extract(*_args, **_kwargs):
            called["n"] += 1
            return None

        monkeypatch.setattr(
            "genesis.learning.pipeline.extract_procedure", fake_extract,
        )
        router = MagicMock()
        router.route_call = AsyncMock()
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.SUCCESS),
            delta_assessor=_make_delta_assessor(),
            observation_writer=MagicMock(write=AsyncMock(return_value="o")),
            router=router,
        )
        await pipeline(FakeCCOutput(), "q", "terminal")
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_approach_failure_extracts_regardless_of_channel(self, db, monkeypatch):
        """APPROACH_FAILURE extraction is channel-agnostic (pre-existing
        behavior — failures are rare enough to always capture)."""
        called = {"n": 0}

        async def fake_extract(*_args, **_kwargs):
            called["n"] += 1
            return None

        monkeypatch.setattr(
            "genesis.learning.pipeline.extract_procedure", fake_extract,
        )
        router = MagicMock()
        router.route_call = AsyncMock()
        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_make_triage_classifier(TriageDepth.FULL_ANALYSIS),
            outcome_classifier=_make_outcome_classifier(OutcomeClass.APPROACH_FAILURE),
            delta_assessor=_make_delta_assessor(),
            observation_writer=MagicMock(write=AsyncMock(return_value="o")),
            router=router,
        )
        await pipeline(FakeCCOutput(), "q", "terminal")
        assert called["n"] == 1
