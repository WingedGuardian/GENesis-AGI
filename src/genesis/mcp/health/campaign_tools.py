"""MCP tools for the campaign subsystem.

Provides campaign_create, campaign_list, campaign_status, campaign_pause,
campaign_resume, campaign_trigger, and campaign_update.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Late-bound state — set by init_campaign_tools()
_runner = None
_db = None


def init_campaign_tools(*, runner, db) -> None:
    """Wire campaign tools to their runtime dependencies."""
    global _runner, _db
    _runner = runner
    _db = db
    logger.info("Campaign MCP tools wired")


# ---------------------------------------------------------------------------
# Implementation functions (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_campaign_create(
    name: str,
    strategy_doc_path: str,
    cron_cadence: str,
    *,
    model: str = "sonnet",
    effort: str = "medium",
    profile: str = "interact",
    pre_checks: list[str] | None = None,
    max_daily_cost_usd: float = 1.0,
    initial_state: dict | None = None,
) -> dict:
    """Create and activate a new campaign."""
    if _db is None:
        return {"error": "Database not available"}

    from genesis.db.crud import campaigns as crud

    existing = await crud.get_campaign_by_name(_db, name)
    if existing:
        return {"error": f"Campaign '{name}' already exists"}

    campaign_id = str(uuid.uuid4())
    checks = json.dumps(pre_checks or ["rate_limit", "budget", "slots_available"])
    state = json.dumps(initial_state or {})

    await crud.create_campaign(
        _db,
        id=campaign_id,
        name=name,
        strategy_doc_path=strategy_doc_path,
        cron_cadence=cron_cadence,
        model=model,
        effort=effort,
        session_profile=profile,
        pre_checks=checks,
        max_daily_cost_usd=max_daily_cost_usd,
        state_json=state,
        created_at=datetime.now(UTC).isoformat(),
    )

    # Schedule the campaign if runner is available
    if _runner:
        campaign = await crud.get_campaign(_db, campaign_id)
        await _runner.add_campaign(campaign)

    return {"id": campaign_id, "name": name, "status": "active"}


async def _impl_campaign_list(status_filter: str | None = None) -> dict:
    """List campaigns with status, last run, and health."""
    if _db is None:
        return {"error": "Database not available"}

    from genesis.db.crud import campaigns as crud

    campaigns = await crud.list_campaigns(_db, status_filter=status_filter)
    items = []
    for c in campaigns:
        items.append({
            "name": c["name"],
            "status": c["status"],
            "cadence": c["cron_cadence"],
            "last_run": c.get("last_run_at"),
            "total_runs": c["total_runs"],
            "total_cost": f"${c['total_cost_usd']:.2f}",
            "model": c["model"],
        })
    return {"campaigns": items, "count": len(items)}


async def _impl_campaign_status(name: str) -> dict:
    """Detailed status for a single campaign."""
    if _db is None:
        return {"error": "Database not available"}

    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(_db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    runs = await crud.list_runs(_db, campaign["id"], limit=5)
    state = {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        state = json.loads(campaign["state_json"])

    # Filter internal keys from state display
    visible_state = {k: v for k, v in state.items() if not k.startswith("_")}

    return {
        "name": campaign["name"],
        "status": campaign["status"],
        "cadence": campaign["cron_cadence"],
        "model": campaign["model"],
        "effort": campaign["effort"],
        "profile": campaign["session_profile"],
        "max_daily_cost": f"${campaign['max_daily_cost_usd']:.2f}",
        "state": visible_state,
        "last_run": campaign.get("last_run_at"),
        "total_runs": campaign["total_runs"],
        "total_cost": f"${campaign['total_cost_usd']:.2f}",
        "recent_runs": [
            {
                "started": r["started_at"],
                "outcome": r["outcome"],
                "summary": r.get("summary", ""),
                "cost": f"${r['cost_usd']:.2f}",
            }
            for r in runs
        ],
    }


async def _impl_campaign_pause(name: str) -> dict:
    """Pause a campaign."""
    if _db is None:
        return {"error": "Database not available"}

    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(_db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    await crud.update_campaign(
        _db, campaign["id"],
        status="paused",
        paused_at=datetime.now(UTC).isoformat(),
    )

    if _runner:
        await _runner.remove_campaign(name)

    return {"name": name, "status": "paused"}


async def _impl_campaign_resume(name: str) -> dict:
    """Resume a paused campaign."""
    if _db is None:
        return {"error": "Database not available"}

    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(_db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    await crud.update_campaign(
        _db, campaign["id"],
        status="active",
        paused_at=None,
    )

    if _runner:
        campaign = await crud.get_campaign(_db, campaign["id"])
        await _runner.add_campaign(campaign)

    return {"name": name, "status": "active"}


async def _impl_campaign_trigger(name: str) -> dict:
    """Manually trigger a campaign tick."""
    if _db is None or _runner is None:
        return {"error": "Campaign runner not available"}

    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(_db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    result = await _runner.campaign_tick(campaign["id"], trigger_type="manual")
    return {"name": name, **result}


async def _impl_campaign_update(
    name: str,
    *,
    cron_cadence: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_daily_cost_usd: float | None = None,
) -> dict:
    """Update campaign configuration."""
    if _db is None:
        return {"error": "Database not available"}

    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(_db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    updates = {}
    if cron_cadence is not None:
        updates["cron_cadence"] = cron_cadence
    if model is not None:
        updates["model"] = model
    if effort is not None:
        updates["effort"] = effort
    if max_daily_cost_usd is not None:
        updates["max_daily_cost_usd"] = max_daily_cost_usd

    if updates:
        await crud.update_campaign(_db, campaign["id"], **updates)

        # Reschedule if cadence changed
        if cron_cadence and _runner:
            await _runner.remove_campaign(name)
            updated = await crud.get_campaign(_db, campaign["id"])
            await _runner.add_campaign(updated)

    return {"name": name, "updated": list(updates.keys())}


# ---------------------------------------------------------------------------
# FastMCP tool decorators
# ---------------------------------------------------------------------------


@mcp.tool()
async def campaign_create(
    name: str,
    strategy_doc_path: str,
    cron_cadence: str,
    model: str = "sonnet",
    effort: str = "medium",
    profile: str = "interact",
    pre_checks: list[str] | None = None,
    max_daily_cost_usd: float = 1.0,
    initial_state: dict | None = None,
) -> dict:
    """Create and activate a new campaign.

    Args:
        name: Unique slug (e.g., 'discord-engagement').
        strategy_doc_path: Path to the strategy markdown file.
        cron_cadence: Cron expression for tick schedule.
        model: LLM model for session ticks (sonnet/opus/haiku).
        effort: LLM effort level (low/medium/high).
        profile: DirectSession profile (observe/interact/research).
        pre_checks: List of pre-check names to run before each tick.
        max_daily_cost_usd: Budget cap per day.
        initial_state: Initial campaign state dict.
    """
    return await _impl_campaign_create(
        name, strategy_doc_path, cron_cadence,
        model=model, effort=effort, profile=profile,
        pre_checks=pre_checks, max_daily_cost_usd=max_daily_cost_usd,
        initial_state=initial_state,
    )


@mcp.tool()
async def campaign_list(status_filter: str | None = None) -> dict:
    """List all campaigns with status and health indicators.

    Args:
        status_filter: Optional filter (active/paused/completed/failed).
    """
    return await _impl_campaign_list(status_filter)


@mcp.tool()
async def campaign_status(name: str) -> dict:
    """Detailed status of a campaign: state, recent runs, cost.

    Args:
        name: Campaign name slug.
    """
    return await _impl_campaign_status(name)


@mcp.tool()
async def campaign_pause(name: str) -> dict:
    """Pause a campaign (stops scheduling, keeps state).

    Args:
        name: Campaign name slug.
    """
    return await _impl_campaign_pause(name)


@mcp.tool()
async def campaign_resume(name: str) -> dict:
    """Resume a paused campaign.

    Args:
        name: Campaign name slug.
    """
    return await _impl_campaign_resume(name)


@mcp.tool()
async def campaign_trigger(name: str) -> dict:
    """Manually trigger a campaign tick (bypasses schedule).

    Args:
        name: Campaign name slug.
    """
    return await _impl_campaign_trigger(name)


@mcp.tool()
async def campaign_update(
    name: str,
    cron_cadence: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_daily_cost_usd: float | None = None,
) -> dict:
    """Update campaign configuration.

    Args:
        name: Campaign name slug.
        cron_cadence: New cron expression.
        model: New LLM model.
        effort: New effort level.
        max_daily_cost_usd: New daily budget cap.
    """
    return await _impl_campaign_update(
        name, cron_cadence=cron_cadence, model=model,
        effort=effort, max_daily_cost_usd=max_daily_cost_usd,
    )
