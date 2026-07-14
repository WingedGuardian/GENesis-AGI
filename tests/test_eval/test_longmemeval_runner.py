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


def _r(qid, qtype, correct, evidence=True, coverage=None):  # noqa: FBT002
    return QuestionArmResult(
        question_id=qid,
        question_type=qtype,
        arm_label="raw",
        hypothesis="h",
        judged_correct=correct,
        evidence_recalled=evidence,
        evidence_coverage=coverage,
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


def test_build_run_summary_reports_mean_evidence_coverage():
    # coverage is |evidence ∩ recalled| / |evidence| per question; abstention
    # questions (no evidence turns) carry None and are EXCLUDED from the mean,
    # never counted as 0 — they have nothing to cover.
    results = [
        _r("q1", "multi-session", True, coverage=1.0),
        _r("q2", "multi-session", False, coverage=0.5),
        _r("q3_abs", "single-session-user", True, evidence=False, coverage=None),
    ]
    summary = build_run_summary(
        arm_label="raw",
        model="m",
        dataset="longmemeval_oracle",
        results=results,
    )
    assert summary.scores["evidence_coverage_mean"] == 0.75


def test_build_run_summary_no_coverage_key_when_all_none():
    results = [_r("q1_abs", "single-session-user", True, evidence=False, coverage=None)]
    summary = build_run_summary(
        arm_label="raw",
        model="m",
        dataset="longmemeval_oracle",
        results=results,
    )
    assert "evidence_coverage_mean" not in summary.scores


def test_write_dump_jsonl_round_trips_per_question_fields():
    import json as _json

    from genesis.eval.longmemeval.runner import write_dump_jsonl

    results = [
        QuestionArmResult(
            question_id="q1",
            question_type="temporal-reasoning",
            arm_label="raw",
            hypothesis="About six weeks.",
            judged_correct=True,
            evidence_recalled=True,
            evidence_coverage=0.5,
            query="how many weeks ago did I leave my old job",
            recalled_ids=("m-1", "m-2"),
            judge_raw="yes",
            input_tokens=10,
            output_tokens=2,
            latency_ms=12.5,
        ),
    ]
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "raw.jsonl"
        write_dump_jsonl(path, results, skipped_case_ids=["q9"])
        lines = [_json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    rec = lines[0]
    assert rec["question_id"] == "q1"
    assert rec["question_type"] == "temporal-reasoning"
    assert rec["arm"] == "raw"
    assert rec["query"] == "how many weeks ago did I leave my old job"
    assert rec["recalled_ids"] == ["m-1", "m-2"]
    assert rec["evidence_coverage"] == 0.5
    assert rec["hypothesis"] == "About six weeks."
    assert rec["judged_correct"] is True
    assert rec["judge_raw"] == "yes"
    skip_rec = lines[1]
    assert skip_rec["question_id"] == "q9"
    assert skip_rec["skipped"] is True


def test_build_run_summary_skipped_do_not_tilt_accuracy():
    # 1 correct + 1 wrong attempted, 2 errored-out (skipped): accuracy is over
    # the 2 ATTEMPTED (0.5), not diluted by the skips, but total/skipped reflect them.
    results = [
        _r("q1", "multi-session", True),
        _r("q2", "multi-session", False),
    ]
    summary = build_run_summary(
        arm_label="raw",
        model="m",
        dataset="longmemeval_oracle",
        results=results,
        skipped_case_ids=["q3", "q4"],
    )
    assert summary.total_cases == 4
    assert summary.skipped_cases == 2
    assert summary.passed_cases == 1
    assert summary.failed_cases == 1
    assert summary.aggregate_score == 0.5  # 1 / 2 attempted, not 1 / 4
    assert sum(1 for s in summary.results if s.skipped) == 2
