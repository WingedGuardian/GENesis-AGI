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
from datetime import UTC, datetime, timedelta
from typing import Any

# State parsing + model/effort resolution live in control.py (shared with the
# MCP tools and dashboard routes) so there is one source of truth.
from genesis.campaigns.control import (
    parse_state as _parse_state,
)
from genesis.campaigns.control import (
    resolve_effort as _resolve_effort,
)
from genesis.campaigns.control import (
    resolve_model as _resolve_model,
)

logger = logging.getLogger(__name__)

# How often the pending-session reaper checks for finished sessions to capture.
# Short interval (IntervalTrigger resetting on restart is harmless at this
# cadence — it just fires again shortly after startup).
_REAPER_INTERVAL_SECONDS = 120
# Grace window before a still-pending run is eligible for orphan reconciliation.
# Protects a run that was just created but whose state pointer is still being
# written (the create_run -> update_campaign_state dispatch window).
_ORPHAN_GRACE_SECONDS = 300

# Campaign names the runner cannot host, because it registers a job of its OWN
# under the same derived scheduler id.
#
# ``start()`` schedules every campaign as ``campaign_{name}`` and THEN registers
# the reaper as ``campaign_pending_reaper``, both with ``replace_existing=True``.
# So a campaign actually named ``pending_reaper`` is silently evicted at startup
# — no error, no log, it simply never ticks again. The failure is invisible
# precisely because ``replace_existing`` is the behaviour we want everywhere
# else (it makes re-registration idempotent).
#
# Exported rather than checked inline so the write boundary and the startup heal
# enforce the SAME set: a reserved name that only one of them knows about is how
# this reopens.
PENDING_REAPER_NAME = "pending_reaper"
RESERVED_CAMPAIGN_NAMES = frozenset({PENDING_REAPER_NAME})


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

        # Pending-session reaper: captures finished sessions promptly instead of
        # waiting for the next (possibly days-apart) cron tick, and reconciles
        # orphaned pending runs. Registered once, independent of any campaign.
        from apscheduler.triggers.interval import IntervalTrigger

        self._scheduler.add_job(
            self._reap_pending_sessions,
            IntervalTrigger(seconds=_REAPER_INTERVAL_SECONDS),
            id=f"campaign_{PENDING_REAPER_NAME}",
            max_instances=1,
            misfire_grace_time=60,
            replace_existing=True,
        )

        self._scheduler.start()
        logger.info(
            "CampaignRunner started with %d active campaign(s) + pending reaper",
            len(campaigns),
        )

    async def stop(self) -> None:
        """Shutdown the scheduler."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("CampaignRunner stopped")

    def _schedule_campaign(self, camp: dict) -> None:
        """Add an APScheduler job for a campaign."""
        from apscheduler.triggers.cron import CronTrigger

        from genesis.env import user_timezone

        try:
            trigger = CronTrigger.from_crontab(
                camp["cron_cadence"], timezone=user_timezone()
            )
        except (ValueError, KeyError) as exc:
            logger.error(
                "Invalid cron for campaign %s: %s", camp["name"], exc
            )
            return

        # Optional jitter (seconds) for randomized fire times. from_crontab()
        # does not accept a jitter kwarg, so set it on the built trigger.
        jitter = camp.get("jitter_seconds")
        if jitter:
            trigger.jitter = int(jitter)

        job_id = f"campaign_{camp['name']}"
        self._scheduler.add_job(
            self._tick_wrapper,
            trigger,
            args=[camp["id"], job_id],
            id=job_id,
            max_instances=1,
            misfire_grace_time=300,
            replace_existing=True,
        )

    async def _tick_wrapper(self, campaign_id: str, job_id: str) -> None:
        """APScheduler entry point — wraps campaign_tick with error handling.

        Records job health (success / failure) via the runtime so a tick crash
        is observable through job_health → JobHealthCollector → ego/dashboard,
        not just the server log. Mirrors the surplus scheduler's pattern. The
        recording is best-effort (suppressed) and never propagates to
        APScheduler — the tick-level swallow contract is preserved.
        """
        from genesis.runtime import GenesisRuntime

        try:
            await self.campaign_tick(campaign_id)
        except Exception as exc:
            logger.exception("Campaign tick failed for %s", campaign_id)
            with contextlib.suppress(Exception):
                GenesisRuntime.instance().record_job_failure(job_id, exc=exc)
            return
        with contextlib.suppress(Exception):
            GenesisRuntime.instance().record_job_success(job_id)

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
            "db": self._db,
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
        channel_history = await _recent_channel_posts(self._db, days=7)
        prompt = _build_session_prompt(
            campaign, state, recent_runs, strategy_text,
            channel_history=channel_history,
        )

        from genesis.cc.direct_session import DirectSessionRequest

        request = DirectSessionRequest(
            prompt=prompt,
            system_prompt=strategy_text,
            profile=campaign.get("session_profile", "campaign"),
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

    async def _reap_pending_sessions(self) -> None:
        """Capture finished pending sessions + reconcile orphans for all campaigns.

        Runs on a short interval so a campaign's completed DirectSession is
        captured within minutes rather than waiting for its next cron tick
        (which may be days away). Best-effort per campaign — one failure never
        blocks the others.
        """
        from genesis.db.crud import campaigns as crud

        try:
            # Only campaigns with pending runs need capture/reconcile — keeps
            # the reaper's cost proportional to in-flight work, not the total
            # (incl. dead/paused) campaign count.
            campaigns = await crud.list_campaigns_with_pending_runs(self._db)
        except Exception:
            logger.warning("Pending reaper: failed to list campaigns", exc_info=True)
            return

        for camp in campaigns:
            try:
                await self._reap_one(camp)
            except Exception:
                logger.warning(
                    "Pending reaper: failed for campaign %s",
                    camp.get("name"), exc_info=True,
                )

    async def _reap_one(self, campaign: dict) -> None:
        """Capture one campaign's finished session and reconcile its orphans."""
        from genesis.db.crud import campaigns as crud

        campaign_id = campaign["id"]
        state = _parse_state(campaign["state_json"])
        pending_session_id = state.get("_pending_session_id")
        pending_run_id = state.get("_pending_run_id")

        if pending_session_id:
            result = await _check_session_status(self._db, pending_session_id)
            if result is not None:
                # Session finished — capture now (idempotent vs. the cron tick).
                await _capture_session_results(
                    self._db, campaign, state, result, pending_run_id
                )
                # Reload to learn the post-capture pending marker (if any).
                refreshed = await crud.get_campaign(self._db, campaign_id)
                if refreshed:
                    state = _parse_state(refreshed["state_json"])

        # Reconcile orphaned pending runs (superseded dispatches), keeping the
        # currently-active pending run and any run younger than the grace window.
        now = datetime.now(UTC)
        keep = state.get("_pending_run_id")
        reconciled = await crud.mark_orphan_runs_superseded(
            self._db,
            campaign_id,
            keep,
            older_than=(now - timedelta(seconds=_ORPHAN_GRACE_SECONDS)).isoformat(),
            finished_at=now.isoformat(),
        )
        if reconciled:
            logger.info(
                "Pending reaper reconciled %d orphan run(s) for %s",
                reconciled, campaign["name"],
            )


# ── Helpers ──────────────────────────────────────────────────────────────


def _read_strategy_doc(path: str) -> str:
    """Read a strategy doc from disk. Returns empty string on failure."""
    try:
        with open(path) as f:
            return f.read()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("Failed to read strategy doc %s: %s", path, exc)
        return ""


# Sentinel: _follow_park could not produce a verdict (no park row / lookup
# error) — the caller falls through to the session's own failed result.
_NO_PARK_VERDICT = object()

# How long a campaign will wait on an open (parked/resuming) rate-limit park
# before treating the work as failed. Generous — real capacity droughts span
# days — but bounded, so a resume loop stuck on a deterministic non-rate-limit
# failure (which never increments attempts → never reaches needs_user) cannot
# stall a campaign forever.
_PARK_WAIT_BOUND = timedelta(days=7)
# A resuming park whose claim is younger than this may have a LIVE queued/
# running delivery attempt — cancelling the row would not stop it, so
# abandonment holds until the attempt terminates on its own. Matches the
# resume engine's stale-claim reclaim bound (_STALE_RESUMING_S = 2h).
_ACTIVE_CLAIM_BOUND = timedelta(hours=2)


def _parse_iso(raw: str) -> datetime | None:
    """Parse an ISO timestamp defensively; naive values are assumed UTC."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _follow_park(db: Any, park_id: str) -> dict | None | object:
    """Resolve a rate-limit park into a campaign-visible session verdict.

    Returns:
        None — park open (parked/resuming) or resumed-but-not-yet-delivered:
            the work is still in flight, keep waiting.
        dict — a terminal verdict: the delivering session's real result
            (park resumed + a finished re-dispatched session found), or a
            failure when the park itself went terminal (needs_user /
            cancelled / expired).
        _NO_PARK_VERDICT — park row missing/unreadable: no opinion.
    """
    try:
        cursor = await db.execute(
            "SELECT status, created_at, claimed_at, updated_at "
            "FROM cc_rate_limit_parks WHERE id = ?",
            (park_id,),
        )
        park_row = await cursor.fetchone()
        if not park_row:
            return _NO_PARK_VERDICT
        park_status = str(park_row["status"])

        if park_status in ("parked", "resuming"):
            # Bounded wait (audit F1): a resume loop stuck on a genuine
            # (non-rate-limit) failure can cycle parked→resuming forever
            # without ever reaching needs_user. A campaign must not skip its
            # ticks indefinitely on that — past the bound, surface a real
            # failure so the next tick dispatches fresh work.
            created = _parse_iso(str(park_row["created_at"] or ""))
            # Unparseable/missing created_at counts as OVER the bound — a
            # corrupt row must not reopen the unbounded-wait trajectory.
            if created is None or (datetime.now(UTC) - created) > _PARK_WAIT_BOUND:
                # An ACTIVELY-claimed resuming attempt may already have a
                # queued/live delivery session; cancelling the ROW would not
                # cancel that session → the exact double-run this bound
                # prevents. Hold until the attempt terminates on its own
                # (<= the resume engine's 2h reclaim bound), then abandon on
                # a later tick.
                claimed = _parse_iso(str(park_row["claimed_at"] or ""))
                if (
                    park_status == "resuming"
                    and claimed is not None
                    and (datetime.now(UTC) - claimed) <= _ACTIVE_CLAIM_BOUND
                ):
                    return None
                # Close the park when abandoning it: leaving it open would let
                # the resume engine execute the SAME campaign action later,
                # after the campaign has already moved on (double-run). The
                # abandonment verdict is returned ONLY when the cancel is
                # CONFIRMED — on a transient failure the campaign stays
                # attached and retries next tick (never fail open into a
                # detached-but-runnable park).
                try:
                    from genesis.db.crud import cc_rate_limit_parks as _parks

                    # Conditional cancel (TOCTOU guard): fires only if the row
                    # still holds the (status, claimed_at) we read above. A resume
                    # tick that advanced the park in this window (incl. a
                    # recover→re-claim that lands back on 'resuming' with a fresh
                    # claim) makes this no-op — return None and stay attached so we
                    # never clobber a live resume into 'cancelled' (double-run).
                    cancelled = await _parks.mark_terminal_if_unchanged(
                        db,
                        park_id,
                        "cancelled",
                        expected_status=park_status,
                        expected_claimed_at=park_row["claimed_at"],
                        expected_updated_at=park_row["updated_at"],
                    )
                except Exception:
                    logger.warning(
                        "Failed to cancel over-bound park %s — staying "
                        "attached; will retry next tick",
                        park_id,
                        exc_info=True,
                    )
                    return None
                if not cancelled:
                    # The park moved under us (a resume tick advanced it) — the
                    # cancel decision is stale. Stay attached; re-evaluate next tick.
                    return None
                return {
                    "success": False,
                    "output_text": (
                        f"rate-limit park {park_id} still {park_status} after "
                        f"{_PARK_WAIT_BOUND.days} days — wait bound exceeded; "
                        "treating the parked campaign work as failed"
                    ),
                    "cost_usd": 0.0,
                }
            return None  # still in flight — the resume engine owns it

        if park_status == "resumed":
            # Find the re-dispatched session that delivered (its metadata
            # carries caller_context "rate_limit_resume:<park_id>"; the
            # closing quote pins the id boundary inside the JSON string).
            cursor = await db.execute(
                """SELECT status, cost_usd, metadata FROM cc_sessions
                   WHERE metadata LIKE ?
                   ORDER BY started_at DESC LIMIT 1""",
                (f'%rate_limit_resume:{park_id}"%',),
            )
            deliv_row = await cursor.fetchone()
            if not deliv_row:
                # A park is marked resumed only AFTER its delivering session
                # is persisted (mark_resumed_if_lineage runs post-_store_result)
                # — a missing row means deleted/corrupted, never in-flight.
                # Returning None here would skip campaign ticks FOREVER.
                return {
                    "success": False,
                    "output_text": (
                        f"rate-limit park {park_id} is resumed but its "
                        "delivering session row is missing (deleted or "
                        "corrupted) — treating the parked campaign work as "
                        "failed"
                    ),
                    "cost_usd": 0.0,
                }
            deliv = dict(deliv_row)
            dstatus = str(deliv.get("status", ""))
            if dstatus not in ("completed", "failed"):
                return None  # delivering session still running
            dmeta = {}
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                dmeta = json.loads(deliv.get("metadata") or "{}")
            return {
                "success": dstatus == "completed",
                "output_text": dmeta.get("output_text", ""),
                "cost_usd": deliv.get("cost_usd", 0.0) or 0.0,
            }

        # needs_user / cancelled / expired — genuinely terminal without
        # delivery: surface as a real failure naming the park state.
        return {
            "success": False,
            "output_text": (
                f"rate-limit park {park_id} ended {park_status} without "
                "delivering — the parked campaign work was not completed"
            ),
            "cost_usd": 0.0,
        }
    except Exception:
        logger.warning("Failed to follow park %s", park_id, exc_info=True)
        return _NO_PARK_VERDICT


async def _check_session_status(db: Any, session_id: str) -> dict | None:
    """Check if a DirectSession has completed.

    Returns:
        dict — session result if completed (success or failure)
        None — session is still running (found in DB with active status)

    If the session is not found in the DB (pruned, lost), returns a
    synthetic failure result so the campaign doesn't stall permanently.
    """
    try:
        from genesis.db.crud import cc_sessions

        row = await cc_sessions.get_by_id(db, session_id)
        if not row:
            # Session not found — treat as completed with error to avoid
            # permanent stall. This can happen if sessions are pruned by
            # cleanup while a campaign tick is pending.
            logger.warning(
                "Campaign pending session %s not found in DB — treating as failed",
                session_id,
            )
            return {
                "success": False,
                "output_text": "",
                "cost_usd": 0.0,
            }

        status = row.get("status", "")
        if status in ("completed", "failed"):
            metadata = {}
            raw_meta = row.get("metadata", "")
            if raw_meta:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    metadata = json.loads(raw_meta)

            # Rate-limit-park awareness (2026-08-17 incident: a parked
            # campaign session was captured as outcome=error while its resume
            # later delivered — the campaign's bookkeeping never learned).
            # A failed session stamped with a park_id is NOT a real failure:
            # follow the park.
            park_id = metadata.get("park_id")
            if status == "failed" and park_id:
                park_result = await _follow_park(db, str(park_id))
                if park_result is not _NO_PARK_VERDICT:
                    return park_result

            return {
                "success": status == "completed",
                "output_text": metadata.get("output_text", ""),
                "cost_usd": row.get("cost_usd", 0.0),
            }
    except Exception:
        # Surfaced at WARNING (not DEBUG): a persistent failure here silently
        # stalls capture (caller treats None as "still running"), so it must be
        # visible at production log levels.
        logger.warning(
            "Session status check failed for %s", session_id, exc_info=True
        )

    return None


async def _capture_session_results(
    db: Any,
    campaign: dict,
    state: dict,
    session_result: dict,
    pending_run_id: str | None,
) -> None:
    """Capture completed session results into campaign state.

    Idempotent under concurrency: the cron tick and the pending-session reaper
    can both call this for the same run. The run row is *claimed* first via an
    optimistic lock (``complete_run(only_if_pending=True)``); if another path
    already captured it, this returns before mutating any campaign state or
    totals, so nothing is double-counted.
    """
    from genesis.db.crud import campaigns as crud

    output_text = session_result.get("output_text", "")
    cost_usd = session_result.get("cost_usd", 0.0)
    success = session_result.get("success", False)

    # Capture session ID before any state mutation
    completed_session_id = state.get("_pending_session_id")

    # Build the summary (no state mutation yet)
    parsed = _extract_json(output_text)
    if parsed:
        summary = parsed.get("summary", "")
    else:
        # Couldn't parse — use raw text as summary
        summary = output_text[:500] if output_text else "No output"

    # ── Claim the run FIRST (optimistic lock). Bail before touching state if
    #    a concurrent capture path already completed it. ──
    if pending_run_id:
        claimed = await crud.complete_run(
            db, pending_run_id,
            outcome="success" if success else "error",
            summary=summary,
            cost_usd=cost_usd,
            session_id=completed_session_id,
            finished_at=datetime.now(UTC).isoformat(),
            only_if_pending=True,
        )
        if claimed == 0:
            # Already captured by the other path — do not re-apply state/totals.
            return

    # ── We won the claim — merge validated state updates and clear markers. ──
    if parsed:
        state_updates = parsed.get("state_updates", {})
        # Filter out internal keys before validation
        current_public = {k: v for k, v in state.items() if not k.startswith("_")}
        validated = _validate_state_updates(state_updates, current_public)
        state.update(validated)

    state.pop("_pending_session_id", None)
    state.pop("_pending_run_id", None)
    await crud.update_campaign_state(db, campaign["id"], json.dumps(state))

    # Atomic counter bump (only when a run was actually claimed/counted).
    if pending_run_id:
        await crud.increment_campaign_totals(db, campaign["id"], cost_usd)


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
    # Defensive: an LLM may emit `"state_updates": null` (or a list/string)
    # rather than an object. parsed.get("state_updates", {}) returns that
    # non-dict as-is, so guard here — otherwise .items() raises AttributeError
    # inside the (already-claimed) capture path and the update is lost.
    if not isinstance(updates, dict):
        return {}
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


async def _recent_channel_posts(db: Any, *, days: int = 7) -> list[dict]:
    """Fetch recent outreach_history entries for non-Telegram channels.

    Gives campaign sessions visibility into what has already been posted,
    preventing duplicate content.
    """
    try:
        cursor = await db.execute(
            "SELECT signal_type, topic, channel, delivered_at "
            "FROM outreach_history "
            "WHERE channel != 'telegram' AND delivered_at IS NOT NULL "
            "AND delivered_at >= datetime('now', ?) "
            "ORDER BY delivered_at DESC LIMIT 20",
            (f"-{days} days",),
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in await cursor.fetchall()]
    except Exception:
        logger.debug("Failed to fetch channel history for campaign prompt", exc_info=True)
        return []


def _build_session_prompt(
    campaign: dict,
    state: dict,
    recent_runs: list[dict],
    strategy_text: str,
    *,
    channel_history: list[dict] | None = None,
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

    if channel_history:
        lines = []
        for post in channel_history:
            lines.append(
                f"- [{post['delivered_at']}] {post['signal_type']} → "
                f"#{post['channel']}: {post['topic']}"
            )
        parts.append(
            "\n## Recent Channel Activity (already posted — do NOT repeat)\n"
            + "\n".join(lines)
        )

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


