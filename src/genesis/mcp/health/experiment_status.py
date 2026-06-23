"""Experiment status MCP tool — read-only surface for cognitive A/B results.

The recommend-only delivery surface for the Phase-7 experimentation harness.
It reads persisted experiment runs (``eval_runs``, trigger='experiment') and
surfaces, per experiment, the control vs treatment scores, the win-rate stats,
and the recommendation. It NEVER promotes a variant — ``autonomous_action`` is
always False; a human acts on a recommendation manually (``settings_update`` for
flag knobs, ``signal_weights.update_weight`` for weights).
"""

from __future__ import annotations

import json
import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _meta(row: dict) -> dict:
    raw = row.get("metadata_json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def _impl_experiment_status(limit: int = 10) -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    db = _service._db
    from genesis.eval.db import get_experiment_runs

    # Fetch both arms per experiment (control + treatment rows).
    rows = await get_experiment_runs(db, limit=limit * 2)
    by_id = {r["id"]: r for r in rows}

    experiments = []
    evo_runs = []
    for r in rows:
        meta = _meta(r)
        if meta.get("kind") == "evo_summary":
            # Evo loop run summary (one row per run) — surfaced separately.
            if len(evo_runs) < limit:
                evo_runs.append({
                    "evo_run": r.get("dataset"),  # "evo:reflection:<id>"
                    "created_at": r.get("created_at"),
                    "winner": meta.get("winner"),
                    "winner_approach": meta.get("winner_approach"),
                    "survivors": meta.get("survivors"),
                    "candidates_evaluated": meta.get("candidates_evaluated"),
                    "holdout_disjoint": meta.get("holdout_disjoint"),
                    "note": meta.get("note"),
                })
            continue
        if meta.get("arm") != "treatment":
            continue  # the treatment row carries the comparison + win-rate
        control = by_id.get(r.get("comparison_run_id")) or {}
        control_meta = _meta(control)
        experiments.append({
            "experiment": r.get("dataset"),  # "experiment:reflection:<name>"
            "created_at": r.get("created_at"),
            "recommendation": meta.get("recommendation"),
            "winrate": meta.get("winrate", {}),
            "n_cases": r.get("total_cases"),
            "control": {
                "variant": control_meta.get("variant"),
                "mean_score": control.get("aggregate_score"),
                "n_pass": control.get("passed_cases"),
            },
            "treatment": {
                "variant": meta.get("variant"),
                "mean_score": r.get("aggregate_score"),
                "n_pass": r.get("passed_cases"),
            },
            "judge_provider": meta.get("judge_provider"),
            "errors": meta.get("errors"),
        })
        if len(experiments) >= limit:
            break

    return {
        "status": "ok",
        "autonomous_action": False,
        "experiments": experiments,
        "evo_runs": evo_runs,
        "note": (
            "Recommend-only: the harness NEVER promotes a variant. Act on a "
            "recommendation manually — settings_update for flag knobs, "
            "signal_weights.update_weight for awareness weights. "
            "recommendation ∈ {treatment_wins, control_wins, no_difference, "
            "insufficient_data}."
        ),
    }


@mcp.tool()
async def experiment_status(limit: int = 10) -> dict:
    """Latest cognitive A/B experiment results + promotion recommendations.

    Read-only surface for the Phase-7 experimentation harness: per experiment,
    the control vs treatment mean scores, the paired win-rate (McNemar exact),
    and the recommendation. The harness is RECOMMEND-ONLY — ``autonomous_action``
    is always False; nothing is promoted automatically.
    """
    return await _impl_experiment_status(limit=limit)
