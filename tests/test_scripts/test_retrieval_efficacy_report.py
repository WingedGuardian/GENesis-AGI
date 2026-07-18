"""Retrieval-efficacy report: pure build_report over synthetic snapshots
(trend join, reconstructed-pool fallback for pre-WS2-0 weeks, drift detection,
LongMemEval arm summary) + markdown rendering. No DB, no network."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "retrieval_efficacy_report", _SCRIPTS_DIR / "retrieval_efficacy_report.py"
)
_rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rep)


def _snap(period_end: str, **metrics) -> dict:
    return {"period_end": period_end, "metrics": metrics}


def test_build_report_trend_is_chronological_and_uses_snapshot_pool():
    # get_snapshots returns newest-first; the trend must come out oldest-first.
    snapshots = [
        _snap(
            "2026-07-17",
            precision_at_5=0.8,
            hit_rate=0.7,
            mrr=0.6,
            total_recalls=40,
            pool_episodic_total=55000,
        ),
        _snap(
            "2026-07-10",
            precision_at_5=0.75,
            hit_rate=0.72,
            mrr=0.55,
            total_recalls=30,
            pool_episodic_total=52000,
        ),
    ]
    report = _rep.build_report(
        snapshots=snapshots,
        reconstructed_pool={},
        lme_runs=[],
    )
    trend = report["trend"]
    assert [r["period_end"] for r in trend] == ["2026-07-10", "2026-07-17"]
    assert trend[0]["pool_episodic_total"] == 52000
    assert trend[0]["pool_source"] == "snapshot"
    assert trend[1]["hit_rate"] == 0.7


def test_build_report_falls_back_to_reconstructed_pool_for_pre_ws2_0_weeks():
    # A pre-WS2-0 snapshot has no pool_* — the reconstructed count fills it,
    # tagged so a reader knows it is an approximation.
    snapshots = [_snap("2026-06-01", precision_at_5=0.6, hit_rate=0.6)]
    report = _rep.build_report(
        snapshots=snapshots,
        reconstructed_pool={"2026-06-01": 40000},
        lme_runs=[],
    )
    row = report["trend"][0]
    assert row["pool_episodic_total"] == 40000
    assert row["pool_source"] == "reconstructed"


def test_build_report_pool_unknown_when_neither_source_available():
    snapshots = [_snap("2026-06-01", hit_rate=0.6)]
    report = _rep.build_report(snapshots=snapshots, reconstructed_pool={}, lme_runs=[])
    row = report["trend"][0]
    assert row["pool_episodic_total"] is None
    assert row["pool_source"] == "unknown"


def test_build_report_drift_flags_grew_and_dropped():
    snapshots = [
        _snap("2026-07-17", hit_rate=0.4, pool_episodic_total=55000),
        _snap("2026-06-01", hit_rate=0.69, pool_episodic_total=20000),
    ]
    report = _rep.build_report(snapshots=snapshots, reconstructed_pool={}, lme_runs=[])
    drift = report["drift"]
    assert drift is not None
    assert drift["pool_grew"] is True
    assert drift["quality_dropped"] is True
    assert drift["first_period"] == "2026-06-01"
    assert drift["last_period"] == "2026-07-17"


def test_build_report_drift_none_with_fewer_than_two_usable_weeks():
    # only one week carries both a quality number and a pool size
    snapshots = [
        _snap("2026-07-17", hit_rate=0.7, pool_episodic_total=55000),
        _snap("2026-07-10", hit_rate=None),  # unusable
    ]
    report = _rep.build_report(snapshots=snapshots, reconstructed_pool={}, lme_runs=[])
    assert report["drift"] is None


def test_build_report_longmemeval_arms_latest_per_arm():
    runs = [  # newest-first
        {"model_profile": "longmemeval:raw", "aggregate_score": 0.65, "created_at": "2026-07-17"},
        {
            "model_profile": "longmemeval:raw",
            "aggregate_score": 0.60,
            "created_at": "2026-07-10",
        },  # older, must be ignored
        {
            "model_profile": "longmemeval:raw+scope",
            "aggregate_score": 0.68,
            "created_at": "2026-07-17",
        },
        {
            "model_profile": "bench:genesis",
            "aggregate_score": 0.9,
            "created_at": "2026-07-17",
        },  # not longmemeval → excluded
    ]
    report = _rep.build_report(snapshots=[], reconstructed_pool={}, lme_runs=runs)
    arms = {a["arm"]: a["aggregate_score"] for a in report["longmemeval_arms"]}
    assert arms == {"raw": 0.65, "raw+scope": 0.68}


def test_render_md_smoke_empty_and_populated():
    # empty
    empty = _rep.build_report(snapshots=[], reconstructed_pool={}, lme_runs=[])
    md_empty = _rep.render_md(empty, generated_at="2026-07-17T00:00:00Z")
    assert "Retrieval-efficacy report" in md_empty
    assert "no weekly memory snapshots yet" in md_empty
    assert "UNTESTED" in md_empty  # honest premise caveat

    # populated
    snapshots = [
        _snap("2026-07-17", hit_rate=0.4, pool_episodic_total=55000),
        _snap("2026-06-01", hit_rate=0.69, pool_episodic_total=20000),
    ]
    report = _rep.build_report(snapshots=snapshots, reconstructed_pool={}, lme_runs=[])
    md = _rep.render_md(report, generated_at="2026-07-17T00:00:00Z")
    assert "Drift check" in md
    assert "drift" in md.lower()
    assert "flip-gate checklist" in md
