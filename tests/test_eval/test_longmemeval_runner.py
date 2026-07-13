"""Tests for LongMemEval arm labeling + result aggregation (WS-1 A4).

The orchestration (build store -> ingest -> recall -> answer -> judge) is
verified by the real end-to-end oracle run. These tests pin the pure logic:
arm labels and the per-arm / per-question-type aggregation into an
``EvalRunSummary``.
"""

from __future__ import annotations

from genesis.eval.longmemeval.query import QueryArm
from genesis.eval.longmemeval.runner import (
    Arm,
    QuestionArmResult,
    build_run_summary,
    default_arms,
)
from genesis.eval.types import ScorerType


def test_arm_label():
    assert Arm(QueryArm.RAW, rerank=False).label == "raw"
    assert Arm(QueryArm.RAW, rerank=True).label == "raw+rerank"
    assert Arm(QueryArm.KEYWORD, rerank=False).label == "keyword"
    assert Arm(QueryArm.KEYWORD, rerank=True).label == "keyword+rerank"


def test_default_arms_are_the_four_combinations():
    labels = {a.label for a in default_arms()}
    assert labels == {"raw", "raw+rerank", "keyword", "keyword+rerank"}


def _r(qid, qtype, correct, evidence=True):  # noqa: FBT002
    return QuestionArmResult(
        question_id=qid,
        question_type=qtype,
        arm_label="raw",
        hypothesis="h",
        judged_correct=correct,
        evidence_recalled=evidence,
        judge_raw="yes" if correct else "no",
        input_tokens=10,
        output_tokens=2,
    )


def test_build_run_summary_aggregates_overall_and_per_type():
    results = [
        _r("q1", "single-session-user", True),
        _r("q2", "single-session-user", False),
        _r("q3", "temporal-reasoning", True),
        _r("q4", "temporal-reasoning", True),
    ]
    summary = build_run_summary(
        arm_label="raw",
        model="openai/gpt-4o-2024-08-06",
        dataset="longmemeval_oracle",
        results=results,
    )
    assert summary.total_cases == 4
    assert summary.passed_cases == 3
    assert summary.failed_cases == 1
    assert summary.aggregate_score == 0.75
    assert summary.model_profile == "longmemeval:raw"
    # per-question-type accuracies land in scores
    assert summary.scores["single-session-user"] == 0.5
    assert summary.scores["temporal-reasoning"] == 1.0


def test_build_run_summary_records_evidence_recall_rate():
    results = [
        _r("q1", "single-session-user", True, evidence=True),
        _r("q2", "single-session-user", False, evidence=False),
    ]
    summary = build_run_summary(
        arm_label="raw",
        model="m",
        dataset="longmemeval_oracle",
        results=results,
    )
    assert summary.scores["evidence_recall_rate"] == 0.5


def test_build_run_summary_scored_outputs_use_llm_judge():
    results = [_r("q1", "multi-session", True)]
    summary = build_run_summary(
        arm_label="raw",
        model="m",
        dataset="longmemeval_oracle",
        results=results,
    )
    (scored,) = summary.results
    assert scored.case_id == "q1"
    assert scored.passed is True
    assert scored.scorer_type == ScorerType.LLM_JUDGE
    assert "multi-session" in scored.scorer_detail


def test_build_run_summary_empty_results_is_safe():
    summary = build_run_summary(
        arm_label="keyword",
        model="m",
        dataset="longmemeval_oracle",
        results=[],
    )
    assert summary.total_cases == 0
    assert summary.aggregate_score == 0.0
