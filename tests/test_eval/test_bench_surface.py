"""Tests for the bench A/B read-surface shaping (eval/bench/surface.py).

Pure functions — no DB, no I/O. Fixtures mirror the real
``eval_runs.metadata_json`` shape a genesis-arm bench row carries (aggregate
scores + versions only; never per-task private text).
"""

from __future__ import annotations

import json

from genesis.eval.bench.surface import build_bench_surface, summarize_bench_run


def _genesis_row(
    *,
    run_id: str = "b2be8b5fad67-genesis",
    created_at: str = "2026-07-10T04:47:52.637239+00:00",
    aggregate: float = 0.8111,
    with_stats: bool = True,
    invalid: bool = False,
) -> dict:
    """A row as returned by eval.db.get_bench_comparisons (metadata_json is a
    JSON *string*, exactly like the DB column)."""
    meta: dict = {
        "bench": True,
        "arm": "genesis",
        "judge_calibrated": False,
        "rubric": "bench_task_success",
        "rubric_version": "1.0.0",
        "task_set_version": "pilot-v1",
        "task_file_sha256": "c39463" + "0" * 58,
        "effort": "medium",
        "invalid": invalid,
        "paired_run_id": "b2be8b5fad67-bare",
    }
    if with_stats:
        meta["stats"] = {
            "score_winrate": {
                "n_cases": 9,
                "control_mean_score": 0.6444,
                "treatment_mean_score": 0.8111,
                "mean_delta": 0.1667,
                "n_control_wins": 0,
                "n_treatment_wins": 2,
                "n_ties": 7,
                "n_discordant": 2,
                "p_value": 0.5,
                "significant": False,
                "recommendation": "insufficient_data",
            },
            "pass_winrate": {
                "control_pass_rate": 0.6667,
                "treatment_pass_rate": 0.7778,
                "recommendation": "insufficient_data",
            },
        }
    return {
        "id": run_id,
        "model_profile": "bench:genesis",
        "aggregate_score": aggregate,
        "comparison_run_id": "b2be8b5fad67-bare",
        "created_at": created_at,
        "metadata_json": json.dumps(meta),
    }


def test_summarize_extracts_headline():
    out = summarize_bench_run(_genesis_row())
    assert out["has_stats"] is True
    assert out["bare_mean"] == 0.6444
    assert out["genesis_mean"] == 0.8111
    assert out["mean_delta"] == 0.1667
    assert out["genesis_wins"] == 2
    assert out["bare_wins"] == 0
    assert out["ties"] == 7
    assert out["n_cases"] == 9
    assert out["p_value"] == 0.5
    assert out["recommendation"] == "insufficient_data"
    assert out["judge_calibrated"] is False
    assert out["task_set_version"] == "pilot-v1"
    assert out["rubric_version"] == "1.0.0"
    assert out["invalid"] is False


def test_summarize_carries_pass_rates():
    out = summarize_bench_run(_genesis_row())
    assert out["pass_rate_bare"] == 0.6667
    assert out["pass_rate_genesis"] == 0.7778


def test_summarize_handles_missing_stats_no_crash():
    # The live all-zero ceff0733 run: metadata present but no stats populated.
    out = summarize_bench_run(
        _genesis_row(run_id="ceff07333378-genesis", aggregate=0.0, with_stats=False, invalid=True),
    )
    assert out["has_stats"] is False
    assert out["invalid"] is True
    assert out["bare_mean"] is None
    assert out["genesis_mean"] is None
    # Non-stats fields still surface (never crash).
    assert out["task_set_version"] == "pilot-v1"
    assert out["judge_calibrated"] is False
    assert out["run_id"] == "ceff07333378-genesis"


def test_summarize_handles_none_metadata_no_crash():
    row = _genesis_row()
    row["metadata_json"] = None
    out = summarize_bench_run(row)
    assert out["has_stats"] is False
    assert out["judge_calibrated"] is False
    assert out["bare_mean"] is None


def test_build_surface_empty():
    surface = build_bench_surface([])
    assert surface["count"] == 0
    assert surface["latest"] is None
    assert surface["series"] == []
    assert surface["judge_calibrated"] is False


def test_build_surface_latest_prefers_run_with_stats():
    # Newest row first (as get_bench_comparisons returns). Newest is invalid /
    # no-stats; the latest headline must fall back to the newest run WITH stats.
    newest_invalid = _genesis_row(
        run_id="newer-genesis",
        created_at="2026-07-11T00:00:00+00:00",
        aggregate=0.0,
        with_stats=False,
        invalid=True,
    )
    older_valid = _genesis_row(
        run_id="older-genesis",
        created_at="2026-07-10T00:00:00+00:00",
    )
    surface = build_bench_surface([newest_invalid, older_valid])
    assert surface["count"] == 2
    assert surface["latest"]["run_id"] == "older-genesis"
    assert surface["latest"]["has_stats"] is True
    # series preserves input order (newest first) and includes the invalid run.
    assert [s["run_id"] for s in surface["series"]] == ["newer-genesis", "older-genesis"]
    assert surface["judge_calibrated"] is False


def test_build_surface_latest_falls_back_when_no_stats_anywhere():
    only_invalid = _genesis_row(
        run_id="inv-genesis",
        aggregate=0.0,
        with_stats=False,
        invalid=True,
    )
    surface = build_bench_surface([only_invalid])
    assert surface["count"] == 1
    # No run has stats → latest is the newest run, flagged has_stats False.
    assert surface["latest"]["run_id"] == "inv-genesis"
    assert surface["latest"]["has_stats"] is False
