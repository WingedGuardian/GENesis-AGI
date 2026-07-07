"""build_lane_status MCP tool — the autonomous capability-build lane surface.

One read-only view of what the lane has done: draft build PRs awaiting review,
the items Genesis declined to build (with reasons), greenlight cards still
awaiting a tap, and the verdict-vs-decision calibration record.

Read-only. Reuses existing CRUD; no new schema; does NOT change behaviour.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


async def _impl_build_lane_status() -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    db = _service._db
    from genesis.db.crud import build_candidates as bc
    from genesis.env import build_lane_enabled

    open_prs = await bc.list_by_outcome(db, "pr_opened")
    dont_build = await bc.list_by_verdict(db, "dont_build", limit=25)
    counts = await bc.verdict_decision_counts(db)
    # Open (undecided) build verdicts with a card out = pending greenlights.
    # list_open also returns undecided dont_build/needs_discussion rows, which
    # are NOT awaiting a tap, so filter to carded build rows.
    open_undecided = await bc.list_open(db)
    pending_greenlights = [
        r for r in open_undecided
        if r.get("verdict") == "build" and r.get("approval_request_id")
    ]

    return {
        "status": "ok",
        "enabled": build_lane_enabled(),
        "open_prs": [
            {
                "title": r.get("item_title"),
                "pr_url": r.get("pr_url"),
                "branch": r.get("branch"),
                "created_at": r.get("created_at"),
            }
            for r in open_prs
        ],
        "wouldnt_build": [
            {
                "title": r.get("item_title"),
                "reason": r.get("verdict_reason"),
                "created_at": r.get("created_at"),
            }
            for r in dont_build
        ],
        "pending_greenlights": [
            {"title": r.get("item_title"), "created_at": r.get("created_at")}
            for r in pending_greenlights
        ],
        "calibration": [
            {
                "verdict": r.get("verdict"),
                "user_decision": r.get("user_decision"),
                "count": r.get("count"),
            }
            for r in counts
        ],
        "note": (
            "Autonomous capability-build lane. open_prs = built, a draft PR is "
            "awaiting your review/merge; wouldnt_build = declined verdicts "
            "(reported, never queued); pending_greenlights = carded, awaiting "
            "your tap; calibration = verdict vs your actual decision "
            "(user_decision NULL = still open; dont_build is never carded, so "
            "its rows are uncontested, not agreed). Read-only — does NOT change "
            "behaviour. `enabled` reflects the build_lane.enabled flag."
        ),
    }


@mcp.tool()
async def build_lane_status() -> dict:
    """What has the autonomous capability-build lane done — and how well do its
    verdicts track your decisions?

    Draft build PRs awaiting review/merge, the items Genesis declined to build
    (with reasons), greenlight cards still awaiting a tap, and the
    verdict-vs-decision calibration record. Read-only; does NOT change
    behaviour.
    """
    return await _impl_build_lane_status()
