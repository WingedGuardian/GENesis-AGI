"""Tests for benchmark report rendering and JSON persistence."""

import json

from genesis.eval.bench.report import (
    _fmt_stat,
    render_console,
    report_to_dict,
    write_report,
)
from genesis.eval.bench.types import (
    BenchArmOutcome,
    BenchPair,
    BenchReport,
    BenchTask,
)


def _make_report():
    task = BenchTask(
        id="task-1",
        category="research",
        prompt="Test prompt",
        expected="Test expected result",
    )

    bare = BenchArmOutcome(
        task_id="task-1",
        arm="bare",
        output_text="bare output",
        judge_score=0.7,
    )

    genesis = BenchArmOutcome(
        task_id="task-1",
        arm="genesis",
        output_text="genesis output",
        judge_score=0.8,
    )

    pair = BenchPair(
        task=task,
        bare=bare,
        genesis=genesis,
    )

    return BenchReport(
        run_id="run-123",
        model="test-model",
        effort="medium",
        task_set_version="v1",
        task_file_sha256="abcdef1234567890",
        rubric_name="test-rubric",
        rubric_version="1",
        judge_calibrated=False,
        pairs=[pair],
    )


def test_report_to_dict():
    report = _make_report()

    result = report_to_dict(report)

    assert isinstance(result, dict)
    assert result["run_id"] == "run-123"
    assert result["model"] == "test-model"
    assert result["pairs"][0]["task"]["id"] == "task-1"
    assert result["pairs"][0]["bare"]["judge_score"] == 0.7
    assert result["pairs"][0]["genesis"]["judge_score"] == 0.8

def test_write_report_writes_run_and_output_copies(tmp_path):
    report = _make_report()

    run_dir = tmp_path / "run"
    output_dir = tmp_path / "output"
    run_dir.mkdir()

    returned = write_report(report, run_dir, output_dir)

    run_path = run_dir / "bench_report.json"
    output_path = output_dir / "bench_report_run-123.json"

    assert run_path.exists()
    assert output_path.exists()
    assert returned == output_path

    run_data = json.loads(run_path.read_text(encoding="utf-8"))
    output_data = json.loads(output_path.read_text(encoding="utf-8"))

    assert run_data == output_data
    assert run_data["run_id"] == "run-123"

def test_fmt_stat_empty():
    assert _fmt_stat({}) == "  (no stats — no complete pairs)"


def test_fmt_stat_score_stats():
    stats = {
        "control_mean_score": 0.7,
        "treatment_mean_score": 0.8,
        "mean_delta": 0.1,
        "n_treatment_wins": 3,
        "n_control_wins": 1,
        "n_ties": 1,
        "recommendation": "genesis",
        "p_value": 0.042,
    }

    result = _fmt_stat(stats)

    assert "mean judge score: bare 0.700 → genesis 0.800 (Δ +0.100)" in result
    assert "wins: genesis 3 / bare 1 / ties 1" in result
    assert "verdict: genesis, p=0.042" in result

def test_render_console_basic_and_missing_stats():
    report = _make_report()

    result = render_console(report)

    assert "BENCH run-123 — genesis vs bare (test-model/medium)" in result
    assert "task-1" in result
    assert "bare 0.70" in result
    assert "genesis 0.80" in result
    assert "pairs: 1 complete / 1 total" in result
    assert "(no stats — no complete pairs)" in result

def test_render_console_skipped_pair():
    report = _make_report()

    skipped_task = BenchTask(
        id="task-2",
        category="recall",
        prompt="Skipped prompt",
        expected="Skipped result",
    )

    skipped_bare = BenchArmOutcome(
        task_id="task-2",
        arm="bare",
        output_text="",
        skipped=True,
        skip_reason="timeout",
    )

    skipped_genesis = BenchArmOutcome(
        task_id="task-2",
        arm="genesis",
        output_text="genesis output",
    )

    skipped_pair = BenchPair(
        task=skipped_task,
        bare=skipped_bare,
        genesis=skipped_genesis,
    )

    report.pairs.append(skipped_pair)

    result = render_console(report)

    assert "task-2" in result
    assert "SKIP (bare: timeout)" in result
    assert "pairs: 1 complete / 2 total" in result

def test_render_console_score_stats():
    report = _make_report()

    report.score_winrate = {
        "control_mean_score": 0.700,
        "treatment_mean_score": 0.800,
        "mean_delta": 0.100,
        "n_treatment_wins": 3,
        "n_control_wins": 1,
        "n_ties": 1,
        "recommendation": "genesis",
        "p_value": 0.042,
    }

    result = render_console(report)

    assert "mean judge score: bare 0.700 → genesis 0.800 (Δ +0.100)" in result
    assert "wins: genesis 3 / bare 1 / ties 1" in result
    assert "verdict: genesis, p=0.042" in result

def test_render_console_pass_stats():
    report = _make_report()

    report.pass_winrate = {
        "control_pass_rate": 0.50,
        "treatment_pass_rate": 0.75,
        "n_treatment_wins": 3,
        "n_control_wins": 1,
        "n_concordant_pass": 1,
        "n_concordant_fail": 1,
        "recommendation": "genesis",
        "p_value": 0.12,
    }

    result = render_console(report)

    assert "pass rate: bare 50% → genesis 75%" in result
    assert "wins: genesis 3 / bare 1 / ties 2" in result
    assert "verdict: genesis, p=0.120" in result

def test_render_console_insufficient_data():
    report = _make_report()

    report.score_winrate = {
        "control_mean_score": 0.700,
        "treatment_mean_score": 0.800,
        "mean_delta": 0.100,
        "n_treatment_wins": 1,
        "n_control_wins": 0,
        "n_ties": 0,
        "recommendation": "insufficient_data",
        "p_value": 0.250,
    }

    result = render_console(report)

    assert "verdict: insufficient_data, p=0.250" in result
    assert "PILOT: expected at N≤10" in result
    assert "do NOT quote as significant" in result

def test_render_console_clean_prod_delta():
    report = _make_report()

    report.prod_delta = {
        "clean": True,
        "deltas": [],
    }

    result = render_console(report)

    assert "prod isolation probe: CLEAN" in result

def test_render_console_dirty_prod_delta():
    report = _make_report()

    report.prod_delta = {
        "clean": False,
        "deltas": [
            "table users: +2 rows",
            "cache: modified",
        ],
    }

    result = render_console(report)

    assert (
        "prod isolation probe: "
        "DELTA — attribution required (live prod has ambient writes)"
    ) in result
    assert "    table users: +2 rows" in result
    assert "    cache: modified" in result

def test_render_console_notes_and_persisted_runs():
    report = _make_report()

    report.notes = [
        "pilot run",
        "manual review required",
    ]
    report.control_run_id = "control-123"
    report.treatment_run_id = "treatment-456"

    result = render_console(report)

    assert "note: pilot run" in result
    assert "note: manual review required" in result
    assert (
        "persisted: control=control-123 "
        "treatment=treatment-456 (linked via comparison_run_id)"
    ) in result

