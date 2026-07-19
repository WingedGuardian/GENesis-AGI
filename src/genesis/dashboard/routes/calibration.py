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
    from genesis.db.crud import ledger_predictions as lp_crud

    cells = await cc_crud.list_cells(db, domain=domain, provenance=lane, window_days=window)
    status_counts = {"ok": 0, "thin": 0, "unknown": 0}
    for c in cells:
        status_counts[c["status"]] += 1

    # Pass-level shares from the graded record (same definitions as the
    # grader report / design §7 falsifier): mechanical share over graded
    # non-void rows; fallback share = LLM-graded fraction of resolutions.
    shares = await lp_crud.resolver_share_counts(db)
    graded = shares["graded"]
    return {
        "cells": cells,
        "summary": {
            **status_counts,
            "cell_count": len(cells),
            "graded_total": graded,
            "mechanical_share": (shares["mechanical"] / graded) if graded else None,
            "fallback_share": (shares["llm_fallback"] / graded) if graded else None,
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
    # Parse the raw arg — Flask's type=int silently falls back to the default
    # on coercion failure, which would turn ?window=abc into a 200 for 90d.
    raw_window = request.args.get("window", "90")
    try:
        window = int(raw_window)
    except ValueError:
        window = -1
    if window not in (0, 30, 90):
        return jsonify({"error": "window must be one of [0, 30, 90]"}), 400
    domain = request.args.get("domain") or None

    try:
        return jsonify(await _collect_calibration(rt.db, lane=lane, window=window, domain=domain))
    except Exception:
        logger.exception("Failed to read calibration cells")
        return jsonify({"error": "calibration read failed"}), 500
