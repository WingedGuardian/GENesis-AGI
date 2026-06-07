"""CampaignRunner — APScheduler-based orchestrator for campaign ticks.

Each campaign has a cron-scheduled job. On each tick:
1. Check for pending session results from last tick
2. Run programmatic pre-checks (no LLM)
3. Dispatch a DirectSession if work is warranted
4. Record the run outcome
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class CampaignRunner:
    """Orchestrates campaign ticks. Owns an APScheduler instance."""

    def __init__(
        self,
        db: Any,
        session_runner: Any,
        idle_detector: Any | None = None,
    ) -> None:
        self._db = db
        self._session_runner = session_runner
        self._idle_detector = idle_detector
        self._scheduler = None  # Lazy-init in start()

    async def start(self) -> None:
        """Load active campaigns and schedule their ticks."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        from genesis.db.crud import campaigns as crud

        self._scheduler = AsyncIOScheduler()

        campaigns = await crud.list_campaigns(self._db, status_filter="active")
        for camp in campaigns:
            self._schedule_campaign(camp)

        self._scheduler.start()
        logger.info(
            "CampaignRunner started with %d active campaign(s)", len(campaigns)
        )

    async def stop(self) -> None:
        """Shutdown the scheduler."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("CampaignRunner stopped")

    def _schedule_campaign(self, camp: dict) -> None:
        """Add an APScheduler job for a campaign."""
        from apscheduler.triggers.cron import CronTrigger

        try:
            trigger = CronTrigger.from_crontab(camp["cron_cadence"])
        except (ValueError, KeyError) as exc:
            logger.error(
                "Invalid cron for campaign %s: %s", camp["name"], exc
            )
            return

        self._scheduler.add_job(
            self._tick_wrapper,
            trigger,
            args=[camp["id"]],
            id=f"campaign_{camp['name']}",
            max_instances=1,
            misfire_grace_time=300,
            replace_existing=True,
        )

    async def _tick_wrapper(self, campaign_id: str) -> None:
        """APScheduler entry point — wraps campaign_tick with error handling."""
        try:
            await self.campaign_tick(campaign_id)
        except Exception:
            logger.exception("Campaign tick failed for %s", campaign_id)

    async def campaign_tick(
        self,
        campaign_id: str,
        trigger_type: str = "scheduled",
    ) -> dict:
        """Execute one campaign tick. Returns a result dict.

        Steps:
        1. Load campaign + state
        2. Check for pending session results from prior tick
        3. Run pre-checks
        4. Dispatch a DirectSession
        5. Record the run
        """
        from genesis.db.crud import campaigns as crud

        campaign = await crud.get_campaign(self._db, campaign_id)
        if not campaign:
            return {"outcome": "error", "error": f"Campaign {campaign_id} not found"}

        state = _parse_state(campaign["state_json"])
        now_iso = datetime.now(UTC).isoformat()

        # ── Step 1: Check for pending session from last tick ──
        pending_session_id = state.get("_pending_session_id")
        pending_run_id = state.get("_pending_run_id")

        if pending_session_id:
            session_result = await _check_session_status(self._db, pending_session_id)

            if session_result is None:
                # Session still running — skip this tick
                run_id = str(uuid.uuid4())
                await crud.create_run(
                    self._db, id=run_id, campaign_id=campaign_id,
                    started_at=now_iso, trigger_type=trigger_type,
                )
                await crud.complete_run(
                    self._db, run_id, outcome="skip",
                    skip_reason="pending_session_still_running",
                    finished_at=now_iso,
                )
                return {"outcome": "skip", "skip_reason": "pending_session_still_running"}

            # Session completed — capture results
            await _capture_session_results(
                self._db, campaign, state, session_result, pending_run_id
            )
            # Reload campaign state after capture
            campaign = await crud.get_campaign(self._db, campaign_id)
            state = _parse_state(campaign["state_json"])

        # ── Step 2: Day boundary reset ──
        last_run = campaign.get("last_run_at")
        if last_run:
            state = _day_boundary_reset(state, last_run, now_iso)

        # ── Step 3: Pre-checks ──
        today_str = now_iso[:10]
        daily_cost = await crud.get_daily_cost(self._db, campaign_id, today_str)

        ctx = {
            "daily_cost": daily_cost,
            "session_runner": self._session_runner,
        }

        from genesis.campaigns.prechecks import run_prechecks
        ok, reason = await run_prechecks(campaign, ctx)

        if not ok:
            run_id = str(uuid.uuid4())
            await crud.create_run(
                self._db, id=run_id, campaign_id=campaign_id,
                started_at=now_iso, trigger_type=trigger_type,
            )
            await crud.complete_run(
                self._db, run_id, outcome="skip",
                skip_reason=reason, finished_at=now_iso,
            )
            return {"outcome": "skip", "skip_reason": reason}

        # ── Step 4: Dispatch DirectSession ──
        strategy_text = _read_strategy_doc(campaign["strategy_doc_path"])
        if not strategy_text:
            return {"outcome": "error", "error": "Strategy doc not found or empty"}

        recent_runs = await crud.list_runs(self._db, campaign_id, limit=3)
        prompt = _build_session_prompt(campaign, state, recent_runs, strategy_text)

        from genesis.cc.direct_session import DirectSessionRequest

        request = DirectSessionRequest(
            prompt=prompt,
            system_prompt=strategy_text,
            profile=campaign.get("session_profile", "interact"),
            model=_resolve_model(campaign.get("model", "sonnet")),
            effort=_resolve_effort(campaign.get("effort", "medium")),
            notify=False,  # Campaigns handle their own notifications
            source_tag="campaign",
            caller_context=f"campaign:{campaign_id}",
        )

        session_id = await self._session_runner.spawn(request)

        # Record the run as dispatched
        run_id = str(uuid.uuid4())
        await crud.create_run(
            self._db, id=run_id, campaign_id=campaign_id,
            started_at=now_iso, trigger_type=trigger_type,
            state_snapshot=json.dumps(state),
        )

        # Store pending session info in campaign state
        state["_pending_session_id"] = session_id
        state["_pending_run_id"] = run_id
        await crud.update_campaign_state(self._db, campaign_id, json.dumps(state))
        await crud.update_campaign(self._db, campaign_id, last_run_at=now_iso)

        logger.info(
            "Campaign %s tick dispatched session %s",
            campaign["name"], session_id,
        )

        return {"outcome": "dispatched", "session_id": session_id, "run_id": run_id}

    async def add_campaign(self, campaign: dict) -> None:
        """Register a new campaign and schedule its job."""
        self._schedule_campaign(campaign)

    async def remove_campaign(self, campaign_name: str) -> None:
        """Unschedule a campaign job."""
        job_id = f"campaign_{campaign_name}"
        if self._scheduler and self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse_state(state_json: str) -> dict:
    """Parse campaign state JSON, returning empty dict on failure."""
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        return json.loads(state_json)
    return {}


def _read_strategy_doc(path: str) -> str:
    """Read a strategy doc from disk. Returns empty string on failure."""
    try:
        with open(path) as f:
            return f.read()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("Failed to read strategy doc %s: %s", path, exc)
        return ""


async def _check_session_status(db: Any, session_id: str) -> dict | None:
    """Check if a DirectSession has completed.

    Returns the session result dict if completed, or None if still running.
    """
    try:
        from genesis.db.crud import cc_sessions

        row = await cc_sessions.get_by_id(db, session_id)
        if not row:
            return None

        status = row.get("status", "")
        if status in ("completed", "failed", "error"):
            metadata = {}
            raw_meta = row.get("metadata", "")
            if raw_meta:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    metadata = json.loads(raw_meta)

            return {
                "success": status == "completed",
                "output_text": metadata.get("output_text", ""),
                "cost_usd": row.get("cost_usd", 0.0),
            }
    except Exception:
        logger.debug("Session status check failed for %s", session_id, exc_info=True)

    return None


async def _capture_session_results(
    db: Any,
    campaign: dict,
    state: dict,
    session_result: dict,
    pending_run_id: str | None,
) -> None:
    """Capture completed session results into campaign state."""
    from genesis.db.crud import campaigns as crud

    output_text = session_result.get("output_text", "")
    cost_usd = session_result.get("cost_usd", 0.0)
    success = session_result.get("success", False)

    # Parse structured output
    parsed = _extract_json(output_text)
    summary = ""

    if parsed:
        state_updates = parsed.get("state_updates", {})
        summary = parsed.get("summary", "")

        # Validate and merge state updates
        # Filter out internal keys before validation
        current_public = {k: v for k, v in state.items() if not k.startswith("_")}
        validated = _validate_state_updates(state_updates, current_public)
        state.update(validated)
    else:
        # Couldn't parse — use raw text as summary
        summary = output_text[:500] if output_text else "No output"

    # Clear pending markers
    state.pop("_pending_session_id", None)
    state.pop("_pending_run_id", None)

    # Persist updated state
    await crud.update_campaign_state(db, campaign["id"], json.dumps(state))

    # Complete the pending run
    if pending_run_id:
        await crud.complete_run(
            db, pending_run_id,
            outcome="success" if success else "error",
            summary=summary,
            cost_usd=cost_usd,
            session_id=state.get("_pending_session_id"),
            finished_at=datetime.now(UTC).isoformat(),
        )

    # Update campaign totals
    await crud.update_campaign(
        db, campaign["id"],
        total_runs=campaign["total_runs"] + 1,
        total_cost_usd=campaign["total_cost_usd"] + cost_usd,
    )


def _extract_json(text: str) -> dict | None:
    """3-step JSON extraction from LLM output.

    1. Direct parse
    2. Markdown code block strip
    3. Brace extraction (find first { ... last })

    Returns parsed dict or None.
    """
    if not text:
        return None

    text = text.strip()

    # Step 1: Direct parse
    with contextlib.suppress(json.JSONDecodeError):
        result = json.loads(text)
        if isinstance(result, dict):
            return result

    # Step 2: Markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        with contextlib.suppress(json.JSONDecodeError):
            result = json.loads(match.group(1).strip())
            if isinstance(result, dict):
                return result

    # Step 3: Brace extraction
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        with contextlib.suppress(json.JSONDecodeError):
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result

    return None


def _validate_state_updates(
    updates: dict, current_state: dict
) -> dict:
    """Validate state updates against the current schema.

    Rejects:
    - Keys not present in current_state
    - Value type changes (except None → anything)
    """
    validated = {}
    for key, new_value in updates.items():
        if key not in current_state:
            logger.debug("Rejecting unknown state key: %s", key)
            continue

        current_value = current_state[key]
        # Allow None → any type (initial population)
        if (
            current_value is not None
            and new_value is not None
            and type(current_value) is not type(new_value)
        ):
            logger.debug(
                "Rejecting type change for %s: %s → %s",
                key, type(current_value).__name__, type(new_value).__name__,
            )
            continue

        validated[key] = new_value

    return validated


def _day_boundary_reset(
    state: dict, last_run_iso: str, now_iso: str
) -> dict:
    """Zero daily counters if a day boundary was crossed.

    Convention: keys ending in ``_today`` or ``_daily`` are reset to 0
    when the UTC date changes between last_run and now.
    """
    try:
        last_date = last_run_iso[:10]
        now_date = now_iso[:10]
    except (TypeError, IndexError):
        return state

    if last_date == now_date:
        return state

    reset = dict(state)
    for key, value in state.items():
        if (key.endswith("_today") or key.endswith("_daily")) and isinstance(value, (int, float)):
            reset[key] = type(value)(0)

    return reset


def _build_session_prompt(
    campaign: dict,
    state: dict,
    recent_runs: list[dict],
    strategy_text: str,
) -> str:
    """Build the user-message prompt for the CC session."""
    # Remove internal state keys from what the LLM sees
    visible_state = {k: v for k, v in state.items() if not k.startswith("_")}

    run_summaries = []
    for run in recent_runs[:3]:
        if run.get("summary"):
            run_summaries.append(
                f"- [{run.get('started_at', '?')}] {run['outcome']}: {run['summary']}"
            )

    parts = [
        f"You are executing a tick of the '{campaign['name']}' campaign.",
        "",
        f"## Current State\n```json\n{json.dumps(visible_state, indent=2)}\n```",
    ]

    if run_summaries:
        parts.append("\n## Recent Runs\n" + "\n".join(run_summaries))

    parts.append(
        "\n## Instructions"
        "\nFollow the strategy document. After completing your actions, "
        "output a JSON block with your results:"
        "\n```json"
        '\n{"state_updates": {<key: value pairs to update>}, '
        '"summary": "<what you did this tick>", '
        '"actions_taken": [<list of action strings>]}'
        "\n```"
    )

    return "\n".join(parts)


def _resolve_model(model_str: str) -> Any:
    """Convert model string to CCModel enum."""
    from genesis.cc.types import CCModel

    mapping = {
        "sonnet": CCModel.SONNET,
        "opus": CCModel.OPUS,
        "haiku": CCModel.HAIKU,
    }
    return mapping.get(model_str, CCModel.SONNET)


def _resolve_effort(effort_str: str) -> Any:
    """Convert effort string to EffortLevel enum."""
    from genesis.cc.types import EffortLevel

    mapping = {
        "low": EffortLevel.LOW,
        "medium": EffortLevel.MEDIUM,
        "high": EffortLevel.HIGH,
    }
    return mapping.get(effort_str, EffortLevel.MEDIUM)
