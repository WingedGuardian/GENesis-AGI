"""Tests for genesis.reflection.types — frozen dataclasses and enums."""

import pytest

from genesis.reflection.types import (
    AssessmentDimension,
    ContextBundle,
    CostSummary,
    DeepReflectionJob,
    DeepReflectionOutput,
    DimensionScore,
    MemoryOperation,
    PendingWorkSummary,
    ProcedureStats,
    ProcedureTrend,
    QualityCalibrationOutput,
    SurplusDecision,
    WeeklyAssessmentOutput,
)


class TestDeepReflectionJob:
    def test_enum_values(self):
        assert DeepReflectionJob.MEMORY_CONSOLIDATION == "memory_consolidation"
        assert DeepReflectionJob.SURPLUS_REVIEW == "surplus_review"
        assert DeepReflectionJob.SKILL_REVIEW == "skill_review"
        assert DeepReflectionJob.COST_RECONCILIATION == "cost_reconciliation"
        assert DeepReflectionJob.LESSONS_EXTRACTION == "lessons_extraction"
        assert DeepReflectionJob.COGNITIVE_REGENERATION == "cognitive_regeneration"

    def test_all_values(self):
        assert len(DeepReflectionJob) == 6


class TestAssessmentDimension:
    def test_all_six_dimensions(self):
        assert len(AssessmentDimension) == 6


class TestPendingWorkSummary:
    def test_defaults_no_work(self):
        p = PendingWorkSummary()
        assert not p.has_any_work
        assert p.active_jobs == []

    def test_single_job(self):
        p = PendingWorkSummary(surplus_review=True, surplus_pending=3)
        assert p.has_any_work
        assert p.active_jobs == [DeepReflectionJob.SURPLUS_REVIEW]

    def test_multiple_jobs(self):
        p = PendingWorkSummary(
            memory_consolidation=True,
            surplus_review=True,
            cognitive_regeneration=True,
        )
        assert len(p.active_jobs) == 3

    def test_frozen(self):
        p = PendingWorkSummary()
        with pytest.raises(AttributeError):
            p.surplus_review = True  # type: ignore[misc]


class TestProcedureStats:
    def test_defaults(self):
        ps = ProcedureStats()
        assert ps.total_active == 0
        assert ps.avg_success_rate == 0.0
        assert ps.low_performers == []


class TestCostSummary:
    def test_defaults(self):
        cs = CostSummary()
        assert cs.daily_usd == 0.0
        assert cs.monthly_budget_pct == 0.0

    def test_with_values(self):
        cs = CostSummary(daily_usd=1.5, daily_budget_pct=0.75)
        assert cs.daily_usd == 1.5
        assert cs.daily_budget_pct == 0.75


class TestContextBundle:
    def test_defaults(self):
        cb = ContextBundle()
        assert cb.cognitive_state == ""
        assert cb.recent_observations == []
        assert isinstance(cb.pending_work, PendingWorkSummary)


class TestMemoryOperation:
    def test_construction(self):
        op = MemoryOperation(operation="dedup", target_ids=["a", "b"], reason="similar")
        assert op.operation == "dedup"
        assert len(op.target_ids) == 2


class TestSurplusDecision:
    def test_promote(self):
        sd = SurplusDecision(item_id="s1", action="promote", reason="valuable")
        assert sd.action == "promote"

    def test_discard(self):
        sd = SurplusDecision(item_id="s2", action="discard")
        assert sd.action == "discard"


class TestDeepReflectionOutput:
    def test_defaults(self):
        out = DeepReflectionOutput()
        assert out.observations == []
        assert out.confidence == 0.7
        assert out.focus_next == ""

    def test_with_data(self):
        out = DeepReflectionOutput(
            observations=["obs1"],
            surplus_decisions=[SurplusDecision(item_id="x", action="promote")],
            confidence=0.9,
        )
        assert len(out.surplus_decisions) == 1


class TestDimensionScore:
    def test_construction(self):
        ds = DimensionScore(
            dimension=AssessmentDimension.REFLECTION_QUALITY,
            score=0.8,
            evidence="good",
        )
        assert ds.score == 0.8
        assert ds.data_available is True


class TestWeeklyAssessmentOutput:
    def test_defaults(self):
        out = WeeklyAssessmentOutput()
        assert out.overall_score == 0.0
        assert out.dimensions == []


class TestProcedureTrend:
    def test_construction(self):
        pt = ProcedureTrend(
            procedure_id="p1", task_type="test",
            current_success_rate=0.8, previous_success_rate=0.6,
            trend="improving",
        )
        assert pt.trend == "improving"


class TestQualityCalibrationOutput:
    def test_defaults(self):
        out = QualityCalibrationOutput()
        assert not out.drift_detected
        assert out.quarantine_candidates == []
