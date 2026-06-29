"""MCP tools for the campaign subsystem.

Provides campaign_create, campaign_list, campaign_status, campaign_pause,
campaign_resume, campaign_trigger, and campaign_update.

When running inside the main Genesis server, these tools use the wired
runner/db references.  When running as a CC child process (MCP server),
they fall back to direct DB connections for read/write operations.
Runner-dependent operations (trigger, schedule hot-reload) return
informational messages in standalone mode.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.db.connection import BUSY_TIMEOUT_MS
from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Late-bound state — set by init_campaign_tools()
_runner = None
_db = None

# DB path for fallback connections (matches task_tools.py pattern)
_DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"


def init_campaign_tools(*, runner, db) -> None:
    """Wire campaign tools to their runtime dependencies.

    In runtime mode: both runner and db are provided.
    In standalone MCP mode: runner=None, only db is provided.
    """
    global _runner, _db
    _runner = runner
    _db = db
    logger.info(
        "Campaign MCP tools wired (db=%s, runner=%s)",
        db is not None,
        runner is not None,
    )


async def _get_db() -> aiosqlite.Connection:
    """Open a direct DB connection for MCP fallback reads/writes."""
    db = await aiosqlite.connect(str(_DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return db


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
    profile: str = "research",
    pre_checks: list[str] | None = None,
    max_daily_cost_usd: float = 1.0,
    initial_state: dict | None = None,
) -> dict:
    """Create and activate a new campaign."""
    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("campaign_create DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}

    try:
        from genesis.db.crud import campaigns as crud

        existing = await crud.get_campaign_by_name(db, name)
        if existing:
            return {"error": f"Campaign '{name}' already exists"}

        campaign_id = str(uuid.uuid4())
        checks = json.dumps(pre_checks or ["rate_limit", "budget", "slots_available"])
        state = json.dumps(initial_state or {})

        await crud.create_campaign(
            db,
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

        result: dict = {"id": campaign_id, "name": name, "status": "active"}

        # Schedule the campaign if runner is available
        if _runner:
            campaign = await crud.get_campaign(db, campaign_id)
            await _runner.add_campaign(campaign)
        else:
            result["note"] = (
                "Campaign created in database. "
                "Restart genesis-server to activate scheduling."
            )

        return result
    finally:
        if own_db:
            await db.close()


async def _impl_campaign_list(status_filter: str | None = None) -> dict:
    """List campaigns with status, last run, and health."""
    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("campaign_list DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}

    try:
        from genesis.db.crud import campaigns as crud

        campaigns = await crud.list_campaigns(db, status_filter=status_filter)
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
    finally:
        if own_db:
            await db.close()


async def _impl_campaign_status(name: str) -> dict:
    """Detailed status for a single campaign."""
    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("campaign_status DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}

    try:
        from genesis.campaigns import control
        from genesis.db.crud import campaigns as crud

        campaign = await crud.get_campaign_by_name(db, name)
        if not campaign:
            return {"error": f"Campaign '{name}' not found"}

        runs = await crud.list_runs(db, campaign["id"], limit=5)
        state = control.parse_state(campaign["state_json"])

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
    finally:
        if own_db:
            await db.close()


async def _impl_campaign_pause(name: str) -> dict:
    """Pause a campaign."""
    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("campaign_pause DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}

    try:
        from genesis.campaigns import control

        return await control.pause_campaign(db, _runner, name)
    finally:
        if own_db:
            await db.close()


async def _impl_campaign_resume(name: str) -> dict:
    """Resume a paused campaign."""
    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("campaign_resume DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}

    try:
        from genesis.campaigns import control

        return await control.resume_campaign(db, _runner, name)
    finally:
        if own_db:
            await db.close()


async def _impl_campaign_trigger(name: str) -> dict:
    """Manually trigger a campaign tick.

    Requires the main Genesis server — the campaign runner is not
    available in standalone MCP mode (control returns an informational error).
    """
    # When _runner is set, _db is guaranteed set (both wired by
    # init_campaign_tools); when None, control short-circuits before using db.
    from genesis.campaigns import control

    return await control.trigger_campaign(_db, _runner, name)


async def _impl_campaign_update(
    name: str,
    *,
    cron_cadence: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_daily_cost_usd: float | None = None,
    jitter_seconds: int | None = None,
) -> dict:
    """Update campaign configuration."""
    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("campaign_update DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}

    try:
        from genesis.campaigns import control

        return await control.update_campaign_config(
            db, _runner, name,
            cron_cadence=cron_cadence, model=model, effort=effort,
            max_daily_cost_usd=max_daily_cost_usd, jitter_seconds=jitter_seconds,
        )
    finally:
        if own_db:
            await db.close()


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
    profile: str = "research",
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
        profile: DirectSession profile — any registered profile, e.g.
            observe/research/interact/campaign/steward/automaton-brain.
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
    jitter_seconds: int | None = None,
) -> dict:
    """Update campaign configuration.

    Args:
        name: Campaign name slug.
        cron_cadence: New cron expression.
        model: New LLM model (haiku/sonnet/opus).
        effort: New effort level (low/medium/high).
        max_daily_cost_usd: New daily budget cap.
        jitter_seconds: Randomized fire-time spread in seconds (0/None = off).
    """
    return await _impl_campaign_update(
        name, cron_cadence=cron_cadence, model=model,
        effort=effort, max_daily_cost_usd=max_daily_cost_usd,
        jitter_seconds=jitter_seconds,
    )
