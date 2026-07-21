"""Calibration status MCP tool — the WS-2 unified calibration table, per cell.

Read-only surface over ``calibration_cells`` + ``calibration_cell_history``
(the mechanically-graded ledger record — distinct from ``ego_calibration_status``,
which reads the ego's snapshot ECE). Cold-start honesty is enforced at the
rendering layer: ``thin``/``unknown`` cells NEVER show a bare percentage — they
carry the escalation phrasing instead (design §3.4/§4.3; consumers must treat
unknown calibration as "escalate to the user", not as a number).
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Confidence-vs-reality gap on ok cells that flags a domain as miscalibrated.
_GAP_FLOOR = 0.15
_TOP_N = 5


def _readable(cell: dict) -> str:
    """One human line per cell — escalation phrasing for thin/unknown."""
    key = f"{cell['domain']}/{cell['metric']} [{cell['provenance']}, {_window_label(cell['window_days'])}]"
    if cell["status"] == "ok":
        if cell["mean_confidence"] is not None:
            return (
                f"{key}: says ~{cell['mean_confidence']:.0%} → right "
                f"{cell['shrunk_estimate']:.0%} (n={cell['n']}, brier={cell['brier']:.3f})"
            )
        # tool-lane cells: observed base rate, nothing predicted
        return f"{key}: base rate {cell['base_rate']:.0%} (n={cell['n']}, observed only)"
    if cell["status"] == "thin":
        return (
            f"{key}: thin sample (n={cell['n']}) — calibration not yet "
            "trustworthy; treat as unverified and lean on user judgment"
        )
    return (
        f"{key}: calibration unknown (n={cell['n']}) — escalate to user; "
        "do not discount or trust confidence in this domain yet"
    )


def _window_label(days: int) -> str:
    return "all-time" if days == 0 else f"{days}d"


async def _impl_calibration_status(domain: str = "", include_history: bool = False) -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    db = _service._db
    from genesis.db.crud import calibration_cells as cc_crud

    cells = await cc_crud.list_cells(db, domain=domain or None)
    if not cells:
        return {
            "status": "no_data",
            "message": (
                "No calibration cells yet"
                + (f" for domain={domain!r}" if domain else "")
                + ". Cells are recomputed at the end of each scheduled grading "
                "pass (twice daily) from resolved ledger_predictions rows and "
                "tool_call_outcomes — expected until predictions cross their "
                "deadlines and get graded."
            ),
        }

    # Miscalibration ranking: ok cells only (thin/unknown are never ranked —
    # ranking them would be exactly the bare-percentage trust the design bans).
    ranked = [
        c
        for c in cells
        if c["status"] == "ok"
        and c["provenance"] == "stated"
        and c["mean_confidence"] is not None
        and c["shrunk_estimate"] is not None
    ]
    gaps = sorted(ranked, key=lambda c: c["mean_confidence"] - c["shrunk_estimate"], reverse=True)
    overconfident = [
        {
            "domain": c["domain"],
            "metric": c["metric"],
            "window_days": c["window_days"],
            "gap": round(c["mean_confidence"] - c["shrunk_estimate"], 4),
            "n": c["n"],
        }
        for c in gaps
        if c["mean_confidence"] - c["shrunk_estimate"] >= _GAP_FLOOR
    ][:_TOP_N]
    underconfident = [
        {
            "domain": c["domain"],
            "metric": c["metric"],
            "window_days": c["window_days"],
            "gap": round(c["mean_confidence"] - c["shrunk_estimate"], 4),
            "n": c["n"],
        }
        for c in reversed(gaps)
        if c["shrunk_estimate"] - c["mean_confidence"] >= _GAP_FLOOR
    ][:_TOP_N]

    status_counts = {"ok": 0, "thin": 0, "unknown": 0}
    for c in cells:
        status_counts[c["status"]] += 1

    result: dict = {
        "status": "ok",
        "cell_count": len(cells),
        "status_counts": status_counts,
        "computed_at": cells[0]["computed_at"],
        "cells": cells,
        "cells_readable": [_readable(c) for c in cells],
        "overconfident_domains": overconfident,
        "underconfident_domains": underconfident,
        "earnback": await _earnback_evidence(db),
        "note": (
            "Read-only. thin/unknown cells carry escalation phrasing by design "
            "— never treat them as a usable percentage. Rankings use the "
            "stated lane's shrunk estimates on ok cells only."
        ),
    }
    if include_history:
        if not domain:
            result["history"] = []
            result["history_note"] = "include_history requires a domain filter"
        else:
            result["history"] = await cc_crud.list_history(db, domain=domain)
    return result


# Default evidence window — mirrors AutonomyManager._earnback_window_days
# (autonomy config `earnback.window_days`, default 45). This surface is
# read-only observability; the AUTHORITATIVE earn-back proposal path is the
# ego cadence, which reads the live config.
_EARNBACK_WINDOW_DAYS = 45


async def _earnback_evidence(db) -> dict:
    """E1 earn-back evidence stream (WS-2 P4, design §5.2 — surfaced here
    instead of a ``v_earnback_evidence`` SQL view; declared deviation).

    For every DEMOTED autonomy category (current_level < earned_level):
    windowed success/correction counts from the append-only ``autonomy_events``
    ledger, the windowed Bayesian posterior, and ``evidence_supports_earned``
    (whether recent evidence alone re-reaches the earned level — the same
    predicate ``detect_earnback_candidates`` gates on). Empty list = nothing
    demoted. Never raises — degrades to an ``unavailable`` marker.
    """
    try:
        from genesis.db.crud import autonomy as autonomy_crud

        demoted = []
        for row in await autonomy_crud.list_all(db):
            if row["current_level"] >= row["earned_level"]:
                continue
            successes, corrections = await autonomy_crud.windowed_counts(
                db, row["category"], window_days=_EARNBACK_WINDOW_DAYS
            )
            demoted.append(
                {
                    "category": row["category"],
                    "current_level": row["current_level"],
                    "earned_level": row["earned_level"],
                    "window_days": _EARNBACK_WINDOW_DAYS,
                    # This surface uses the DEFAULT window; the authoritative
                    # gate (ego cadence → detect_earnback_candidates) reads
                    # the live autonomy config and may differ if the operator
                    # changed earnback.window_days.
                    "window_source": "default",
                    "window_successes": successes,
                    "window_corrections": corrections,
                    "posterior": round(autonomy_crud.bayesian_posterior(successes, corrections), 4),
                    "evidence_supports_earned": (
                        autonomy_crud.bayesian_level(successes, corrections) >= row["earned_level"]
                    ),
                    "last_regression_at": row["last_regression_at"],
                }
            )
        return {"demoted_categories": demoted}
    except Exception:  # noqa: BLE001 — observability add-on, never breaks the tool
        logger.debug("earnback evidence read failed", exc_info=True)
        return {"demoted_categories": [], "unavailable": True}


@mcp.tool()
async def calibration_status(domain: str = "", include_history: bool = False) -> dict:
    """Genesis's mechanically-graded calibration record, per cell.

    Reads the WS-2 unified calibration table: one cell per (domain,
    action_class, metric, lane, window) with n, base rate, Brier + Murphy
    decomposition, ECE, and a shrunk estimate. ``domain`` filters exactly or
    by dotted prefix (e.g. ``outreach``); ``include_history`` adds trend
    snapshots (requires a domain). Cells labelled thin/unknown render
    escalation phrasing instead of percentages — that is the cold-start
    honesty contract, not missing data. The ``earnback`` key surfaces the
    E1 evidence stream: windowed graded evidence for every demoted autonomy
    category. Distinct from ``ego_calibration_status``
    (the ego's snapshot ECE surface).
    """
    return await _impl_calibration_status(domain, include_history)
