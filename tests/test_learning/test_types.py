"""Tests for genesis.learning.types — enums and dataclasses."""

from datetime import UTC, datetime

from genesis.learning.types import (
    CalibrationRules,
    DeltaClassification,
    DiscoveryAttribution,
    EngagementOutcome,
    EngagementSignal,
    FallbackChain,
    InteractionSummary,
    MaturityStage,
    OutcomeClass,
    ProcedureMatch,
    RequestDeliveryDelta,
    RetrospectiveResult,
    ScopeEvolution,
    SignalWeightTier,
    TriageDepth,
    TriageResult,
)


def test_outcome_class_values():
    assert OutcomeClass.SUCCESS == "success"
    assert OutcomeClass.WORKAROUND_SUCCESS == "workaround_success"
    assert len(OutcomeClass) == 6


def test_triage_depth_ordering():
    assert TriageDepth.SKIP < TriageDepth.QUICK_NOTE < TriageDepth.FULL_PLUS_WORKAROUND
    assert int(TriageDepth.FULL_ANALYSIS) == 3


def test_delta_classification_values():
    assert len(DeltaClassification) == 4
    assert DeltaClassification.EXACT_MATCH == "exact_match"


def test_discovery_attribution_values():
    assert len(DiscoveryAttribution) == 6


def test_signal_weight_tier():
    assert SignalWeightTier.STRONG == "strong"


def test_engagement_outcome():
    assert EngagementOutcome.ENGAGED == "engaged"


def test_maturity_stage():
    assert MaturityStage.EARLY == "early"
    assert MaturityStage.MATURE == "mature"


def test_interaction_summary_frozen():
    now = datetime.now(UTC)
    s = InteractionSummary(
        session_id="s1", user_text="hi", response_text="hello",
        tool_calls=["t1"], token_count=10, channel="telegram", timestamp=now,
    )
    assert s.session_id == "s1"
    assert s.token_count == 10


def test_triage_result():
    t = TriageResult(depth=TriageDepth.SKIP, rationale="trivial", skipped_by_prefilter=True)
    assert t.skipped_by_prefilter is True


def test_scope_evolution():
    se = ScopeEvolution(original_request="do X", final_delivery="did X+Y", scope_communicated=True)
    assert se.scope_communicated is True


def test_request_delivery_delta():
    d = RequestDeliveryDelta(
        classification=DeltaClassification.OVER_DELIVERY,
        attributions=[DiscoveryAttribution.GENESIS_CAPABILITY],
        scope_evolution=None,
        evidence="added extra feature",
    )
    assert d.classification == "over_delivery"
    assert len(d.attributions) == 1


def test_retrospective_result():
    now = datetime.now(UTC)
    summary = InteractionSummary("s1", "hi", "hello", [], 5, "cli", now)
    triage = TriageResult(TriageDepth.QUICK_NOTE, "brief", False)
    r = RetrospectiveResult(
        summary=summary, triage=triage, outcome=OutcomeClass.SUCCESS,
        delta=None, observations_written=1, procedures_updated=0,
    )
    assert r.observations_written == 1


def test_procedure_match():
    pm = ProcedureMatch(
        procedure_id="p1", task_type="deploy", confidence=0.8,
        success_count=5, failure_count=1, failure_modes=[], workarounds=[],
    )
    assert pm.confidence == 0.8


def test_calibration_rules():
    now = datetime.now(UTC)
    cr = CalibrationRules(examples=[{"a": 1}], rules=["r1"], generated_at=now, source_model="sonnet")
    assert cr.source_model == "sonnet"


def test_engagement_signal():
    es = EngagementSignal(channel="telegram", outcome=EngagementOutcome.ENGAGED, latency_seconds=1.5, evidence="replied")
    assert es.latency_seconds == 1.5


def test_fallback_chain():
    fc = FallbackChain(obstacle_type="timeout", methods=["retry", "skip"], current_index=0)
    assert fc.methods[1] == "skip"
