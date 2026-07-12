"""Bench A/B read surface — Genesis-vs-bare win-rate for the dashboard.

Exposes the persisted `genesis eval bench` (WS-1 A3) results — the paired
Genesis-vs-bare-Claude A/B win-rates — as a JSON series. Aggregate-only
(win-rates + task-set/rubric versions); every summary carries
`judge_calibrated` and the winrate `recommendation`, so provisional pilot
numbers (uncalibrated judge, `insufficient_data` at pilot N) are never rendered
as if significant. Shaping lives in `eval/bench/surface.py` (shared with the
`bench_status` MCP tool).
"""

from __future__ import annotations

import logging

from flask import jsonify

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/eval/bench")
@_async_route
async def eval_bench():
    """Return the recent Genesis-vs-bare bench A/B win-rate series."""
    from genesis.eval.bench.surface import build_bench_surface
    from genesis.eval.db import get_bench_comparisons
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify({"error": "not bootstrapped", "count": 0, "series": []}), 503

    runs = await get_bench_comparisons(rt._db, limit=12)
    return jsonify(build_bench_surface(runs))
