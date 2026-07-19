"""Calibration tab API — read-only view over the WS-2 calibration cells.

One GET route: cells filtered by lane/window/domain plus the summary scalars
the tab header shows (status counts, mechanical share, fallback share).
Read-only and non-sensitive — follows the plain no-auth pattern (vitals),
not the auth-gated references pattern. Data collection lives in
``_collect_calibration`` so tests can await it directly against a real DB
(the ``_async_route`` adapter owns its own event loop and cannot run inside
an async test's loop — the cc-sessions route-test split).
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

_LANES = {"stated", "policy_prior", "all"}


async def _collect_calibration(db, *, lane: str, window: int, domain: str | None = None) -> dict:
    """Cells + summary scalars for one lane/window slice."""
    from genesis.db.crud import calibration_cells as cc_crud

    cells = await cc_crud.list_cells(db, domain=domain, provenance=lane, window_days=window)
    status_counts = {"ok": 0, "thin": 0, "unknown": 0}
    for c in cells:
        status_counts[c["status"]] += 1

    # Pass-level shares from the graded record (same definitions as the
    # grader report / design §7 falsifier): mechanical share over graded
    # non-void rows; fallback share = LLM-graded fraction of resolutions.
    cursor = await db.execute(
        "SELECT "
        "  COALESCE(SUM(resolver IN ('mechanical', 'mechanical_absence')), 0), "
        "  COALESCE(SUM(resolver = 'llm_fallback'), 0), "
        "  COUNT(*) "
        "FROM ledger_predictions "
        "WHERE status IN ('resolved', 'fuzzy_resolved') "
        "AND outcome_value IS NOT NULL"
    )
    mech, fallback, graded = await cursor.fetchone()
    return {
        "cells": cells,
        "summary": {
            **status_counts,
            "cell_count": len(cells),
            "graded_total": graded,
            "mechanical_share": (mech / graded) if graded else None,
            "fallback_share": (fallback / graded) if graded else None,
            "last_computed_at": cells[0]["computed_at"] if cells else None,
        },
    }


@blueprint.route("/api/genesis/calibration")
@_async_route
async def calibration_cells():
    """Calibration cells + summary.

    Query params:
        lane   – stated (default) | policy_prior | all
        window – 90 (default) | 30 | 0 (all-time)
        domain – optional exact-or-dotted-prefix filter
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not ready"}), 503

    lane = request.args.get("lane", "stated")
    if lane not in _LANES:
        return jsonify({"error": f"lane must be one of {sorted(_LANES)}"}), 400
    window = request.args.get("window", 90, type=int)
    domain = request.args.get("domain") or None

    try:
        return jsonify(await _collect_calibration(rt.db, lane=lane, window=window, domain=domain))
    except Exception:
        logger.exception("Failed to read calibration cells")
        return jsonify({"error": "calibration read failed"}), 500
