"""Bench A/B read-surface shaping — persisted eval_runs rows → display dicts.

Pure, side-effect-free. Consumed by BOTH the dashboard route
(`dashboard/routes/bench.py`) and the MCP tool (`mcp/health/bench_status.py`)
so the two surfaces render the same shape from the same code.

Input is a genesis-arm bench row (``model_profile='bench:genesis'``) as returned
by ``eval.db.get_bench_comparisons`` — its ``metadata_json.stats`` carries the
already-computed win-rates, so no join to the paired bare row is needed for the
headline. Aggregate-only: NEVER surface per-task prompts/criteria (those live in
the private run-dir JSON, not in these rows).

Honesty contract: every summary carries ``judge_calibrated`` and the winrate
``recommendation`` (``insufficient_data`` at pilot N) so no consumer can render a
provisional number as if it were significant.
"""

from __future__ import annotations

import json
from typing import Any


def _load_meta(run: dict) -> dict:
    """Parse the row's metadata_json (a JSON string in the DB) → dict, tolerant
    of None / already-dict / malformed."""
    raw = run.get("metadata_json")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def summarize_bench_run(run: dict) -> dict:
    """Shape one genesis-arm bench row into a flat display dict.

    Never raises on a stats-less / invalid row (e.g. an all-skip run whose arms
    scored 0.0 and never populated ``stats``): such a row returns
    ``has_stats=False`` with the headline fields as ``None`` but the version /
    provenance fields still filled.
    """
    meta = _load_meta(run)
    stats = meta.get("stats") or {}
    score = stats.get("score_winrate") or {}
    passwr = stats.get("pass_winrate") or {}
    has_stats = bool(score)

    def _num(d: dict, key: str) -> Any:
        v = d.get(key)
        return v if isinstance(v, (int, float)) else None

    return {
        "run_id": run.get("id"),
        "created_at": run.get("created_at"),
        "task_set_version": meta.get("task_set_version"),
        "rubric": meta.get("rubric"),
        "rubric_version": meta.get("rubric_version"),
        "judge_calibrated": bool(meta.get("judge_calibrated", False)),
        "invalid": bool(meta.get("invalid", False)),
        "has_stats": has_stats,
        # score win-rate (headline)
        "bare_mean": _num(score, "control_mean_score"),
        "genesis_mean": _num(score, "treatment_mean_score"),
        "mean_delta": _num(score, "mean_delta"),
        "genesis_wins": _num(score, "n_treatment_wins"),
        "bare_wins": _num(score, "n_control_wins"),
        "ties": _num(score, "n_ties"),
        "n_cases": _num(score, "n_cases"),
        "p_value": _num(score, "p_value"),
        "significant": (bool(score.get("significant")) if "significant" in score else None),
        "recommendation": score.get("recommendation"),
        # pass win-rate (secondary)
        "pass_rate_bare": _num(passwr, "control_pass_rate"),
        "pass_rate_genesis": _num(passwr, "treatment_pass_rate"),
    }


def build_bench_surface(runs: list[dict]) -> dict:
    """Shape a newest-first list of genesis-arm bench rows into the surface.

    ``latest`` is the newest run WITH populated stats (the latest meaningful
    A/B result); if no run has stats it falls back to the newest run (flagged
    ``has_stats=False``). ``series`` preserves the newest-first input order and
    includes invalid / stats-less runs (flagged) so nothing is silently dropped.
    """
    series = [summarize_bench_run(r) for r in runs]
    if not series:
        return {"count": 0, "latest": None, "series": [], "judge_calibrated": False}

    latest = next((s for s in series if s["has_stats"]), series[0])
    return {
        "count": len(series),
        "latest": latest,
        "series": series,
        "judge_calibrated": bool(latest["judge_calibrated"]),
    }
