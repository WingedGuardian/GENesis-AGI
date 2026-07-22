"""Rate-limit resume engine — re-dispatch parked CC work at its reset time.

The PR-2b consumer of the ``cc_rate_limit_parks`` substrate. On a CronTrigger
tick it reclaims stale claims, finds due parks, and (in ``live`` mode) claims and
re-dispatches each one's actual work through the direct-session queue with
``delivery_mode="result"`` — so the finished answer is delivered back to its
origin conversation via the shipped #1192 delivery path.

**No probe.** Subscription budgets are per-model-tier, so a cheap probe can't
gate an expensive resume — the re-run IS the probe. A still-limited retry hits
the same rate limit, and its background catch (``rate_limit_park``) re-limits the
SAME park in place (attempts+1, backoff), so the ``needs_user`` escalation stays
reachable. The park row is resolved by id via ``caller_context`` — the engine
never marks a park ``resumed`` itself; the retry's own outcome does.

**Gate posture.** A resume only completes work whose initiation was ALREADY
approved: a foreground turn (the user's typed prompt) or a direct_session that
already passed the autonomous-CLI approval gate at its original dispatch. It
creates no new ungated work, so it does not re-enter ``AutonomousCliApprovalGate``.
The resumed session runs under a bounded profile (never exceeding the origin
turn's surface). Master gate: ``cc_rate_limit_resume`` mode + the
``GENESIS_RATE_LIMIT_RESUME_DISABLED`` kill switch.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from genesis.cc import rate_limit_park as park_helpers
from genesis.cc import rate_limit_resume_config as cfg_mod
from genesis.db.crud import cc_rate_limit_parks as parks
from genesis.db.crud import direct_session_queue

logger = logging.getLogger("genesis.cc.rate_limit_resume")

# Reclaim a park stuck 'resuming' longer than this (retry lost to a crash/restart
# between claim and outcome). 2h > the 1h direct_session timeout, so a live retry
# is never yanked out from under itself.
_STALE_RESUMING_S = 7200


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _redispatch(db, park: dict) -> None:
    """Re-enqueue a parked unit of work as a RESULT-delivering direct session."""
    payload = json.loads(park["payload_json"])
    await direct_session_queue.enqueue(
        db,
        prompt=payload.get("prompt", ""),
        profile=payload.get("profile", "research"),
        model=payload.get("model", "sonnet"),
        effort=payload.get("effort", "high"),
        timeout_s=int(payload.get("timeout_s", 3600)),
        roster_model=payload.get("roster_model"),
        notify=False,
        notify_on_failure_only=False,
        delivery_mode="result",
        origin_session_id=park["origin_session_id"],
        caller_context=f"{park_helpers._RESUME_PREFIX}{park['id']}",
    )


async def _alert(rt, *, topic: str, context: str) -> None:
    """Governed (deduped) owner alert — best-effort, never breaks a tick."""
    pipeline = getattr(rt, "_outreach_pipeline", None)
    if pipeline is None:
        return
    try:
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        await pipeline.submit(
            OutreachRequest(
                category=OutreachCategory.ALERT,
                topic=topic,
                context=context,
                salience_score=0.85,
                signal_type="rate_limit_park",
                channel="telegram",
                verbatim=True,
            )
        )
    except Exception:
        logger.warning("rate_limit_resume alert failed", exc_info=True)


def _safe_prompt(payload_json: str) -> str:
    """Best-effort prompt from a park payload — never raises (a corrupt row must
    not sink the whole tick)."""
    try:
        return str(json.loads(payload_json).get("prompt", ""))[:200]
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ""


async def _escalate_needs_user(rt, db) -> None:
    """One deduped alert per park that has exhausted auto-resume attempts.

    Each park is isolated: a single corrupt row (bad payload_json, alert failure)
    must not abort the tick before the due-park loop runs (that would recur every
    10 min and block ALL resumes)."""
    stuck = await parks.list_by_status(db, status="needs_user", limit=50)
    for park in stuck:
        try:
            await _alert(
                rt,
                topic=f"Rate limit park {park['id']}",
                context=(
                    "A rate-limited request could not be auto-resumed after "
                    f"{park['attempts']} attempts and needs you. Original prompt: "
                    f"{_safe_prompt(park['payload_json'])}"
                ),
            )
        except Exception:
            logger.warning(
                "needs_user escalation failed for park %s", park.get("id"), exc_info=True
            )


async def run_resume_tick(rt, *, now: datetime | None = None) -> None:
    """One resume pass. Injectable ``now`` for deterministic tests."""
    now = now or datetime.now(UTC)
    db = getattr(rt, "_db", None)
    if db is None:
        return
    try:
        # Always reclaim stale claims first — even in off/propose_only, a park
        # stranded 'resuming' by a restart must return to 'parked'.
        await parks.recover_stale_resuming(db, max_age_s=_STALE_RESUMING_S)

        mode = cfg_mod.effective_mode()
        if mode == "off":
            rt.record_job_success("rate_limit_resume")
            return

        # Surface parks that gave up, regardless of live/propose_only.
        await _escalate_needs_user(rt, db)

        cfg = cfg_mod.load_config()
        due = await parks.list_due(
            db, now=_iso(now), limit=cfg_mod.knob_int(cfg, "max_due_per_tick")
        )
        if not due:
            rt.record_job_success("rate_limit_resume")
            return

        if mode == "propose_only":
            await _alert(
                rt,
                topic="Rate limit parks ready",
                context=(
                    f"{len(due)} rate-limited request(s) are parked and ready to "
                    "resume. Set cc_rate_limit_resume mode to 'live' to auto-resume "
                    "and deliver them."
                ),
            )
            rt.record_job_success("rate_limit_resume")
            return

        # live: claim + re-dispatch each due park.
        dispatched = 0
        for park in due:
            if not await parks.claim(db, park["id"]):
                continue  # another tick/instance won the claim
            try:
                await _redispatch(db, park)
                dispatched += 1
            except Exception:
                # Dispatch failed (queue/db) — re-open with backoff so it retries
                # rather than stranding in 'resuming'.
                logger.error("Re-dispatch failed for park %s", park["id"], exc_info=True)
                attempts = park["attempts"] + 1
                await parks.relimit(
                    db,
                    park["id"],
                    reset_at=park["reset_at"],
                    next_attempt_at=park_helpers.backoff_next_attempt(attempts, now, cfg),
                    needs_user_at_attempts=cfg_mod.knob_int(cfg, "needs_user_attempts"),
                )
        if dispatched:
            logger.info("rate_limit_resume: re-dispatched %d/%d due park(s)", dispatched, len(due))
        rt.record_job_success("rate_limit_resume")
    except Exception as exc:  # noqa: BLE001 — job boundary
        rt.record_job_failure("rate_limit_resume", str(exc))
        logger.exception("rate_limit_resume tick failed")
