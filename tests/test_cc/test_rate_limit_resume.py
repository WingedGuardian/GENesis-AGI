"""Tests for the rate-limit resume engine (rate_limit_resume.run_resume_tick).

Covers mode gating (off/propose_only/live), due-park claim + re-dispatch with the
RESULT+origin+caller_context contract, dispatch-failure re-park, needs_user
escalation alert, stale-resuming recovery, and empty-state no-op. Real in-memory
db; fake runtime with a mocked outreach pipeline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from genesis.cc.rate_limit_resume import run_resume_tick
from genesis.db.crud import cc_rate_limit_parks as parks
from genesis.db.crud import direct_session_queue as dsq

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
PAST = "2026-07-22T10:00:00+00:00"


def _rt(db):
    rt = MagicMock()
    rt._db = db
    rt._outreach_pipeline = MagicMock()
    rt._outreach_pipeline.submit = AsyncMock()
    rt.record_job_success = MagicMock()
    rt.record_job_failure = MagicMock()
    return rt


async def _seed_due(db, *, prompt="do the thing", origin="orig1", dedup="k"):
    return await parks.upsert_open_park(
        db,
        kind="direct_session",
        dedup_key=dedup,
        payload={
            "prompt": prompt,
            "profile": "research",
            "model": "sonnet",
            "effort": "high",
            "timeout_s": 3600,
            "roster_model": None,
        },
        origin_session_id=origin,
        limit_kind="session",
        raw_signal=None,
        reset_at=None,
        next_attempt_at=PAST,
    )


def _force(monkeypatch, mode):
    monkeypatch.setattr("genesis.cc.rate_limit_resume_config.effective_mode", lambda: mode)


async def test_live_redispatches_due_park_with_result_contract(db, monkeypatch):
    _force(monkeypatch, "live")
    rt = _rt(db)
    pid = await _seed_due(db)
    await run_resume_tick(rt, now=NOW)
    # park is claimed (resuming) — engine does NOT mark resumed; the retry does.
    assert (await parks.get_by_id(db, pid))["status"] == "resuming"
    # exactly one queue row, carrying the delivery contract.
    q = await dsq.claim_next(db)
    assert q is not None
    payload = json.loads(q["payload_json"])
    assert payload["delivery_mode"] == "result"
    assert payload["origin_session_id"] == "orig1"
    assert payload["caller_context"] == f"rate_limit_resume:{pid}"
    assert payload["prompt"] == "do the thing"
    assert payload["profile"] == "research"
    rt.record_job_success.assert_called_with("rate_limit_resume")


async def test_off_mode_does_not_dispatch(db, monkeypatch):
    _force(monkeypatch, "off")
    rt = _rt(db)
    pid = await _seed_due(db)
    await run_resume_tick(rt, now=NOW)
    assert (await parks.get_by_id(db, pid))["status"] == "parked"
    assert await dsq.count_pending(db) == 0
    rt.record_job_success.assert_called_with("rate_limit_resume")


async def test_propose_only_alerts_without_dispatch(db, monkeypatch):
    _force(monkeypatch, "propose_only")
    rt = _rt(db)
    pid = await _seed_due(db)
    await run_resume_tick(rt, now=NOW)
    assert (await parks.get_by_id(db, pid))["status"] == "parked"
    assert await dsq.count_pending(db) == 0
    rt._outreach_pipeline.submit.assert_awaited()  # "ready to resume" ping


async def test_empty_state_no_op(db, monkeypatch):
    _force(monkeypatch, "live")
    rt = _rt(db)
    await run_resume_tick(rt, now=NOW)
    assert await dsq.count_pending(db) == 0
    rt.record_job_success.assert_called_with("rate_limit_resume")


async def test_dispatch_failure_reparks_not_stranded(db, monkeypatch):
    _force(monkeypatch, "live")
    monkeypatch.setattr(
        "genesis.cc.rate_limit_resume.direct_session_queue.enqueue",
        AsyncMock(side_effect=RuntimeError("queue down")),
    )
    rt = _rt(db)
    pid = await _seed_due(db)
    await run_resume_tick(rt, now=NOW)
    row = await parks.get_by_id(db, pid)
    # Re-opened for retry (not stranded in 'resuming'), attempts incremented.
    assert row["status"] == "parked"
    assert row["attempts"] == 1
    rt.record_job_success.assert_called_with("rate_limit_resume")


async def test_needs_user_parks_escalate_alert(db, monkeypatch):
    _force(monkeypatch, "live")
    rt = _rt(db)
    pid = await _seed_due(db)
    await db.execute(
        "UPDATE cc_rate_limit_parks SET status='needs_user' WHERE id=?",
        (pid,),
    )
    await db.commit()
    await run_resume_tick(rt, now=NOW)
    # An alert fired referencing the stuck park.
    rt._outreach_pipeline.submit.assert_awaited()
    call = rt._outreach_pipeline.submit.call_args[0][0]
    assert call.signal_type == "rate_limit_park"


async def test_stale_resuming_reclaimed_each_tick(db, monkeypatch):
    _force(monkeypatch, "off")  # off so we isolate the recovery step
    rt = _rt(db)
    pid = await _seed_due(db)
    await parks.claim(db, pid)
    # Backdate the claim so it's stale.
    await db.execute(
        "UPDATE cc_rate_limit_parks SET claimed_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (pid,),
    )
    await db.commit()
    await run_resume_tick(rt, now=NOW)
    assert (await parks.get_by_id(db, pid))["status"] == "parked"


async def test_tick_failure_records_job_failure(db, monkeypatch):
    _force(monkeypatch, "live")
    monkeypatch.setattr(
        "genesis.cc.rate_limit_resume.parks.list_due",
        AsyncMock(side_effect=RuntimeError("db exploded")),
    )
    rt = _rt(db)
    await _seed_due(db)
    await run_resume_tick(rt, now=NOW)  # must not raise
    rt.record_job_failure.assert_called()


async def test_corrupt_needs_user_park_does_not_block_tick(db, monkeypatch):
    """A needs_user park with malformed payload_json must not abort the tick
    before the due-park loop runs (else it recurs every 10 min forever)."""
    _force(monkeypatch, "live")
    rt = _rt(db)
    # A corrupt needs_user park (bad JSON payload).
    bad = await _seed_due(db, dedup="bad", origin="o-bad")
    await db.execute(
        "UPDATE cc_rate_limit_parks SET status='needs_user', payload_json='{not json' WHERE id=?",
        (bad,),
    )
    await db.commit()
    # A healthy due park that must still be resumed this tick.
    good = await _seed_due(db, dedup="good", origin="o-good", prompt="run me")
    await run_resume_tick(rt, now=NOW)  # must not raise
    # The healthy park was claimed + dispatched despite the corrupt one.
    assert (await parks.get_by_id(db, good))["status"] == "resuming"
    assert await dsq.count_pending(db) == 1
    rt.record_job_success.assert_called_with("rate_limit_resume")
    rt.record_job_failure.assert_not_called()


async def test_wire_rate_limit_resume_registers_job():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    from genesis.runtime.init.rate_limit_resume import _wire_rate_limit_resume

    sched = AsyncIOScheduler()
    _wire_rate_limit_resume(sched, MagicMock())
    sched.start(paused=True)
    try:
        job = sched.get_job("rate_limit_resume")
        assert job is not None
        assert isinstance(job.trigger, CronTrigger)
        assert job.max_instances == 1
    finally:
        sched.shutdown(wait=False)
