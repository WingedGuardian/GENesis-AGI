"""loop_closure_status MCP tool — the self-learning loop health surface.

The umbrella "is the loop actually closed?" view. Two things in one read-only
surface:

1. A per-artifact **funnel** — captured → surfaced → actuated → measured → leak
   — over the existing tables (procedures, observations, reflections,
   follow-ups, ego proposals). It names each OPEN seam honestly: what's captured
   but never acted on or measured. This is the assurance the operator asked for.
2. The **Outcome Bus** section (tiers, per-domain T1 success, ego calibration) —
   subsumes the former ``self_improvement_status`` tool, so there is ONE
   self-learning-health surface instead of two overlapping ones.

Read-only. Reuses existing CRUD; no new schema; does NOT change behaviour.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# A discovered item still un-actuated after this long is counted as a leak
# (the "work goes here to die" signal). 14d mirrors the morning report's
# follow-up staleness rule.
_STALE_DAYS = 14


async def _impl_loop_closure_status() -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    db = _service._db
    from genesis.db.crud import loop_closure as lc
    from genesis.mcp.health.self_improvement_status import (
        _impl_self_improvement_status,
    )

    stale_before = (datetime.now(UTC) - timedelta(days=_STALE_DAYS)).isoformat()

    funnel = [
        await lc.procedure_funnel(db),
        await lc.observation_funnel(db, stale_before=stale_before),
        await lc.reflection_funnel(db),
        await lc.followup_funnel(db, stale_before=stale_before),
        await lc.proposal_funnel(db, stale_before=stale_before),
    ]

    # Honest leak report: name every open seam + non-zero leak.
    open_seams: list[str] = []
    for f in funnel:
        if f.get("loop") == "OPEN" and f["captured"] > 0:
            open_seams.append(
                f"{f['artifact']}: {f['captured']} captured, nothing flowing "
                "through (acted on / measured) — loop OPEN"
            )
        for key, val in f.items():
            if key.startswith("leak_") and isinstance(val, int) and val > 0:
                label = key.removeprefix("leak_").replace("_", " ")
                open_seams.append(f"{f['artifact']}: {val} {label}")

    # Subsumed: the Outcome Bus / calibration section (the "measured" layer for
    # outcome-bearing decisions). Reused, not duplicated.
    outcome_bus = await _impl_self_improvement_status()

    return {
        "status": "ok",
        "funnel": funnel,
        "open_seams": open_seams,
        "outcome_bus": outcome_bus,
        "skills_note": (
            "Skills are file-based; their effectiveness signal (failure_reason) "
            "is a stub → measured=OPEN. Closing that is LC2."
        ),
        "note": (
            "Self-learning loop health. funnel = captured→surfaced→actuated→"
            "measured per artifact; open_seams names what is leaking (captured "
            "but never acted on / measured). Read-only — does NOT change "
            f"behaviour. 'stale' = >{_STALE_DAYS}d un-actuated. Subsumes the "
            "former self_improvement_status (now the outcome_bus section)."
        ),
    }


@mcp.tool()
async def loop_closure_status() -> dict:
    """Is Genesis's self-learning loop actually closed, or is learning falling
    through the cracks?

    Per-artifact funnel — captured → surfaced → actuated → measured → leak —
    across procedures, observations, reflections, follow-ups and ego proposals,
    plus Outcome Bus health (signal tiers, per-domain success, ego calibration
    ECE trend). ``open_seams`` names exactly what is captured but never acted on
    or measured. Read-only; does NOT change behaviour.
    """
    return await _impl_loop_closure_status()
