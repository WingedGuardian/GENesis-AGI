"""Shared campaign control operations.

Pause / resume / trigger / update logic used by BOTH the MCP campaign tools
(`genesis.mcp.health.campaign_tools`) and the dashboard campaign routes
(`genesis.dashboard.routes.campaigns`). Keeping one implementation here avoids
the two surfaces drifting apart.

Contract: every function takes an explicit ``db`` connection and an optional
``runner`` (the live ``CampaignRunner``; ``None`` when no scheduler is wired,
e.g. standalone MCP mode). These functions do NO connection management and NO
standalone fallback — the callers own that. When ``runner`` is ``None`` the DB
is still updated and a ``note`` is returned telling the caller a restart is
needed for the live schedule to reflect the change.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

from genesis.cc.types import VALID_EFFORT_NAMES, VALID_MODEL_NAMES

# The model/effort a campaign session may use is the full CC roster — derived
# from the CCModel / EffortLevel enums so a new tier (e.g. fable) or effort level
# is accepted here automatically. resolve_model / resolve_effort below coerce
# straight to the enum, so the validator and dispatch-time resolution can never
# drift apart.
VALID_MODELS = VALID_MODEL_NAMES
VALID_EFFORTS = VALID_EFFORT_NAMES


def parse_state(state_json: str | None) -> dict:
    """Parse a campaign's ``state_json`` blob, returning {} on any failure.

    Shared by the runner, the MCP tools, and the dashboard routes so all three
    surfaces parse campaign state identically.
    """
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        if state_json:
            return json.loads(state_json)
    return {}


def resolve_model(model_str: str) -> Any:
    """Convert a campaign model string to a CCModel enum (default sonnet)."""
    from genesis.cc.types import CCModel

    try:
        return CCModel(model_str)
    except ValueError:
        return CCModel.SONNET


def resolve_effort(effort_str: str) -> Any:
    """Convert a campaign effort string to an EffortLevel enum (default medium)."""
    from genesis.cc.types import EffortLevel

    try:
        return EffortLevel(effort_str)
    except ValueError:
        return EffortLevel.MEDIUM


def _validate_cadence(cron_cadence: str) -> str | None:
    """Return an error string if the cron expression is invalid, else None."""
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron_cadence)
    except (ValueError, KeyError) as exc:
        return f"Invalid cron cadence '{cron_cadence}': {exc}"
    return None


async def pause_campaign(db: Any, runner: Any | None, name: str) -> dict:
    """Pause a campaign: mark paused in the DB and unschedule its live job."""
    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    await crud.update_campaign(
        db, campaign["id"],
        status="paused",
        paused_at=datetime.now(UTC).isoformat(),
    )

    if runner:
        await runner.remove_campaign(name)

    result: dict = {"name": name, "status": "paused"}
    if not runner:
        result["note"] = (
            "Paused in database. Restart genesis-server for schedule "
            "changes to take effect."
        )
    return result


async def resume_campaign(db: Any, runner: Any | None, name: str) -> dict:
    """Resume a paused campaign: mark active and re-register its live job."""
    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    await crud.update_campaign(
        db, campaign["id"],
        status="active",
        paused_at=None,
    )

    if runner:
        campaign = await crud.get_campaign(db, campaign["id"])
        await runner.add_campaign(campaign)

    result: dict = {"name": name, "status": "active"}
    if not runner:
        result["note"] = (
            "Resumed in database. Restart genesis-server for schedule "
            "changes to take effect."
        )
    return result


async def trigger_campaign(db: Any, runner: Any | None, name: str) -> dict:
    """Manually run one campaign tick. Requires a live runner."""
    if runner is None:
        return {
            "error": (
                "Campaign trigger requires the main Genesis server. "
                "The campaign runner is not available in standalone MCP mode. "
                "Use campaign_status to check campaign state, or restart "
                "genesis-server to run campaigns on schedule."
            ),
        }

    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    result = await runner.campaign_tick(campaign["id"], trigger_type="manual")
    return {"name": name, **result}


async def update_campaign_config(
    db: Any,
    runner: Any | None,
    name: str,
    *,
    cron_cadence: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_daily_cost_usd: float | None = None,
    jitter_seconds: int | None = None,
) -> dict:
    """Update tunable campaign config, validating before persisting.

    A schedule-affecting change (cadence or jitter) hot-reschedules the live
    job when a runner is present, so the change takes effect without a restart.
    """
    from genesis.db.crud import campaigns as crud

    campaign = await crud.get_campaign_by_name(db, name)
    if not campaign:
        return {"error": f"Campaign '{name}' not found"}

    # ── Validate before writing anything ──
    if model is not None and model not in VALID_MODELS:
        return {"error": f"Invalid model '{model}'. Valid: {sorted(VALID_MODELS)}"}
    if effort is not None and effort not in VALID_EFFORTS:
        return {"error": f"Invalid effort '{effort}'. Valid: {sorted(VALID_EFFORTS)}"}
    if cron_cadence is not None and (err := _validate_cadence(cron_cadence)):
        return {"error": err}
    if jitter_seconds is not None and jitter_seconds < 0:
        return {"error": "jitter_seconds must be >= 0"}
    if max_daily_cost_usd is not None and max_daily_cost_usd < 0:
        return {"error": "max_daily_cost_usd must be >= 0"}

    updates: dict = {}
    if cron_cadence is not None:
        updates["cron_cadence"] = cron_cadence
    if model is not None:
        updates["model"] = model
    if effort is not None:
        updates["effort"] = effort
    if max_daily_cost_usd is not None:
        updates["max_daily_cost_usd"] = max_daily_cost_usd
    if jitter_seconds is not None:
        # Store 0 as NULL (no jitter) to keep a single "off" representation.
        updates["jitter_seconds"] = jitter_seconds or None

    if not updates:
        return {"name": name, "updated": []}

    await crud.update_campaign(db, campaign["id"], **updates)

    # Reschedule when a schedule-affecting field changed (cadence OR jitter).
    schedule_changed = cron_cadence is not None or jitter_seconds is not None
    result: dict = {"name": name, "updated": list(updates.keys())}

    if schedule_changed and runner:
        # Only reschedule an active campaign — a paused one has no live job.
        if campaign["status"] == "active":
            await runner.remove_campaign(name)
            updated = await crud.get_campaign(db, campaign["id"])
            await runner.add_campaign(updated)
    elif schedule_changed and not runner:
        result["note"] = (
            "Schedule fields updated in database. Restart genesis-server "
            "for changes to take effect."
        )

    return result
