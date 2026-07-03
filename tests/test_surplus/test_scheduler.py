"""Tests for SurplusScheduler — dispatch loop + brainstorm orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from genesis.db.crud import surplus_tasks
from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.executor import StubExecutor
from genesis.surplus.idle_detector import IdleDetector
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.scheduler import SurplusScheduler, _restart_safe_hourly
from genesis.surplus.types import ComputeTier, TaskType

pytestmark = pytest.mark.asyncio


def _make_scheduler(db, *, idle=True, lmstudio_up=False, enable_code_audits=False):
    idle_detector = IdleDetector()
    if idle:
        idle_detector._last_activity_at = datetime.now(UTC) - timedelta(minutes=30)
    else:
        idle_detector.mark_active()
    compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
    return SurplusScheduler(
        db=db, queue=SurplusQueue(db), idle_detector=idle_detector,
        compute_availability=compute, executor=StubExecutor(),
        enable_code_audits=enable_code_audits,
    ), compute


async def test_restart_safe_hourly_returns_crontrigger_not_interval():
    """Every sub-daily / daily cadence must come back as a CronTrigger (never Interval)."""
    for hours in (4, 6, 12, 24):
        trig = _restart_safe_hourly(hours)
        assert isinstance(trig, CronTrigger)
        assert not isinstance(trig, IntervalTrigger)


async def test_restart_safe_hourly_subdaily_is_stepped_and_daily_differs():
    """4h -> every-4-hours step; >=24h -> a single daily fire (the _recently_completed
    cooldown is the real cadence gate, so the trigger only needs to fire frequently ENOUGH)."""
    sub = str(_restart_safe_hourly(4, minute=10))
    daily = str(_restart_safe_hourly(24, minute=20))
    assert "*/4" in sub
    assert "minute='10'" in sub
    assert sub != daily
    assert "*/24" not in daily  # collapses to a fixed daily hour, not an every-24h step


async def test_long_interval_jobs_use_restart_safe_crontriggers(db):
    """The >1h jobs — schedule_maintenance, schedule_code_index, schedule_code_audit — must
    use CronTrigger, not IntervalTrigger: a >1h IntervalTrigger resets on every restart and
    starves the job (the CLAUDE.md trap). Enumerated as a class, not just the flagged job."""
    sched, _ = _make_scheduler(db, enable_code_audits=True)
    await sched.start()
    try:
        for job_id in ("schedule_maintenance", "schedule_code_index", "schedule_code_audit"):
            job = sched._scheduler.get_job(job_id)
            assert job is not None, f"{job_id} not registered"
            assert isinstance(job.trigger, CronTrigger), f"{job_id} must be CronTrigger"
            assert not isinstance(job.trigger, IntervalTrigger)
    finally:
        sched._scheduler.shutdown(wait=False)


async def test_run_gitnexus_strip_cleans_claude_md(db, tmp_path, monkeypatch):
    """run_gitnexus_strip removes the GitNexus block from CLAUDE.md, not AGENTS.md."""
    from pathlib import Path as _P

    sched, _ = _make_scheduler(db)  # build before patching Path.home

    genesis_dir = tmp_path / "genesis"
    genesis_dir.mkdir()
    block = "<!-- gitnexus:start -->\n# GitNexus\nidx\n<!-- gitnexus:end -->\n"
    (genesis_dir / "CLAUDE.md").write_text("# Real instructions\n\n" + block)
    (genesis_dir / "AGENTS.md").write_text("# Agents\n\n" + block)

    monkeypatch.setattr(_P, "home", lambda: tmp_path)
    await sched.run_gitnexus_strip()

    claude = (genesis_dir / "CLAUDE.md").read_text()
    assert "gitnexus:start" not in claude, "block stripped from CLAUDE.md"
    assert "# Real instructions" in claude, "real content preserved"
    assert "gitnexus:start" in (genesis_dir / "AGENTS.md").read_text(), "AGENTS.md kept"


async def test_dispatch_skips_when_not_idle(db):
    sched, compute = _make_scheduler(db, idle=False)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is False


async def test_dispatch_skips_when_queue_empty(db):
    sched, compute = _make_scheduler(db, idle=True)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is False


async def test_dispatch_reaps_old_terminal_rows(db):
    """dispatch_once age-caps terminal rows (runs before the idle short-circuit)."""
    sched, compute = _make_scheduler(db, idle=False)  # not idle → returns early after reap
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    await surplus_tasks.create(
        db, id="ancient", task_type="brainstorm_self", compute_tier="free_api",
        priority=0.5, drive_alignment="curiosity", created_at=old,
    )
    await surplus_tasks.mark_completed(db, "ancient", completed_at=old)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.dispatch_once()
    assert await surplus_tasks.get_by_id(db, "ancient") is None


async def test_dispatch_processes_task(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "cooperation")
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is True
    assert await queue.pending_count() == 0


async def test_dispatch_writes_staging_entry(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "cooperation")
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.dispatch_once()
    cursor = await db.execute("SELECT COUNT(*) FROM surplus_insights")
    row = await cursor.fetchone()
    assert row[0] == 1


async def test_dispatch_skips_local_30b_when_lmstudio_down(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.LOCAL_30B, 0.8, "curiosity")
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is False
    assert await queue.pending_count() == 1


async def test_dispatch_processes_local_30b_when_lmstudio_up(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.LOCAL_30B, 0.8, "curiosity")
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=True):
        result = await sched.dispatch_once()
    assert result is True
    assert await queue.pending_count() == 0


async def test_dispatch_handles_executor_error(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "cooperation")

    async def _boom(task):
        raise RuntimeError("kaboom")

    sched._executor.execute = _boom
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is False
    # Task should be marked failed, not pending
    assert await queue.pending_count() == 0


# --------------------------------------------------------------------------- #
# Verified-correctness verdict (measurement-only quality judge — producer side)
# --------------------------------------------------------------------------- #
async def _completed_verdict(db) -> str | None:
    cur = await db.execute(
        "SELECT outcome_quality FROM surplus_tasks WHERE status='completed'"
    )
    row = await cur.fetchone()
    return row[0]


async def _dispatch_with_judge(db, task_type, judge_return):
    """Dispatch one ``task_type`` through the stub executor with the quality
    judge patched to return ``judge_return``.

    Intake is stubbed benign — the verdict no longer depends on intake routing
    (it comes from the judge). Returns ``(completed_row, judge_mock)`` so callers
    can assert the persisted columns AND whether the judge was invoked at all.
    """
    from genesis.surplus.intake import IntakeStats

    sched, compute = _make_scheduler(db, idle=True)
    await sched._queue.enqueue(task_type, ComputeTier.FREE_API, 0.8, "curiosity")
    benign = IntakeStats(
        findings_count=1, routed_knowledge=1, routed_observation=0, routed_discard=0,
    )
    judge = AsyncMock(return_value=judge_return)
    with patch.object(
        compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False,
    ), patch(
        "genesis.surplus.intake.run_intake",
        new_callable=AsyncMock, return_value=benign,
    ), patch(
        "genesis.surplus.quality_judge.run_quality_judge", judge,
    ):
        assert await sched.dispatch_once() is True
    cur = await db.execute(
        "SELECT outcome_quality, judge_score, judge_detail "
        "FROM surplus_tasks WHERE status='completed'"
    )
    return await cur.fetchone(), judge


async def test_dispatch_records_useful_when_judge_passes(db):
    detail = '{"judge_score": 0.82, "rationale": "solid"}'
    row, judge = await _dispatch_with_judge(
        db, TaskType.BRAINSTORM_USER, ("useful", 0.82, detail),
    )
    assert row["outcome_quality"] == "useful"
    assert row["judge_score"] == 0.82
    assert row["judge_detail"] == detail
    judge.assert_awaited_once()


async def test_dispatch_records_hollow_when_judge_fails(db):
    row, _ = await _dispatch_with_judge(
        db, TaskType.CODE_AUDIT, ("hollow", 0.30, '{"judge_score": 0.30}'),
    )
    assert row["outcome_quality"] == "hollow"
    assert row["judge_score"] == 0.30


async def test_dispatch_judge_outage_stays_null(db):
    # Judge unavailable → (None, None, None) → positive-only, no false hollow.
    row, _ = await _dispatch_with_judge(
        db, TaskType.WING_AUDIT, (None, None, None),
    )
    assert row["outcome_quality"] is None
    assert row["judge_score"] is None
    assert row["judge_detail"] is None


async def test_dispatch_non_insight_type_skips_judge(db):
    # An action / non-insight type must NOT invoke the judge (cost) and stays NULL.
    row, judge = await _dispatch_with_judge(
        db, TaskType.CODE_INDEX, ("hollow", 0.1, "{}"),
    )
    assert row["outcome_quality"] is None
    judge.assert_not_awaited()


async def test_dispatch_pipeline_intermediate_skips_judge(db):
    # Pipeline-intermediate output feeds the next step, not the KB; excluded.
    row, judge = await _dispatch_with_judge(
        db, TaskType.RESEARCH_QUERY_GEN, ("hollow", 0.1, "{}"),
    )
    assert row["outcome_quality"] is None
    judge.assert_not_awaited()


async def test_dispatch_short_content_skips_judge(db):
    # Output shorter than the 50-char quality gate must NOT invoke the judge —
    # the verdict stays NULL (too little to grade), never hollow.
    from genesis.surplus.types import ExecutorResult

    sched, compute = _make_scheduler(db, idle=True)
    await sched._queue.enqueue(
        TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "curiosity",
    )

    async def _short(task):
        return ExecutorResult(
            success=True,
            content="too short",
            insights=[{"generating_model": "x"}],
        )

    sched._executor.execute = _short
    judge = AsyncMock(return_value=("hollow", 0.1, "{}"))
    with patch.object(
        compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False,
    ), patch("genesis.surplus.quality_judge.run_quality_judge", judge):
        await sched.dispatch_once()
    judge.assert_not_awaited()
    assert await _completed_verdict(db) is None


async def test_dispatch_empty_insights_stays_null(db):
    # NOMINAL / clean-pass (executor returns no insights) is NOT hollow — a
    # monitoring/scan type reporting "nothing noteworthy" is a valid success.
    from genesis.surplus.types import ExecutorResult

    sched, compute = _make_scheduler(db, idle=True)
    await sched._queue.enqueue(
        TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, 0.8, "curiosity",
    )

    async def _nominal(task):
        return ExecutorResult(
            success=True,
            content="All clear — nothing noteworthy to capture in this pass today.",
            insights=[],
        )

    sched._executor.execute = _nominal
    with patch.object(
        compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False,
    ):
        await sched.dispatch_once()
    assert await _completed_verdict(db) is None


async def test_brainstorm_check_schedules_sessions(db):
    sched, compute = _make_scheduler(db, idle=True)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.brainstorm_check()
    # Brainstorm (3): user + self + self_unblock
    assert await sched._queue.pending_count() == 3


async def test_start_and_stop(db):
    sched, compute = _make_scheduler(db, idle=True, enable_code_audits=True)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False), \
         patch.object(sched, "run_gitnexus_strip", new_callable=AsyncMock):
        await sched.start()
    assert sched._scheduler.running is True
    # Verify jobs were registered
    assert sched._scheduler.get_job("surplus_brainstorm_check") is not None
    assert sched._scheduler.get_job("surplus_dispatch") is not None
    assert sched._scheduler.get_job("schedule_code_audit") is not None
    # Brainstorm (3) + code audit (1) + code index (1) + model_eval (1)
    # + maintenance (4) + j9_eval_batch (1) + analytical (1: gap_clustering)
    # + pipeline (2: prompt_effectiveness + anticipatory_research step 1) = 14
    assert await sched._queue.pending_count() == 14
    # Stop should not raise
    await sched.stop()


async def test_start_without_code_audits(db):
    sched, compute = _make_scheduler(db, idle=True, enable_code_audits=False)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False), \
         patch.object(sched, "run_gitnexus_strip", new_callable=AsyncMock):
        await sched.start()
    assert sched._scheduler.running is True
    # Code audit job should NOT be registered
    assert sched._scheduler.get_job("schedule_code_audit") is None
    # Brainstorm (3) + code index (1) + model_eval (1)
    # + maintenance (4) + j9_eval_batch (1) + analytical (1: gap_clustering)
    # + pipeline (2: prompt_effectiveness + anticipatory_research step 1) = 13
    assert await sched._queue.pending_count() == 13
    await sched.stop()


async def test_start_registers_skill_security_scan(db):
    """A wired SkillSecurityScanJob registers its weekly cron job on start().

    Locks the scheduler wiring end-to-end: proves start() resolves CronTrigger /
    user_timezone in scope and registers the job under id 'skill_security_scan'.
    A CronTrigger job does not enqueue surplus tasks, so the pending_count
    assertions in the sibling start tests are unaffected.
    """
    sched, compute = _make_scheduler(db, idle=True)
    sched.set_skill_security_scan_job(AsyncMock())
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.start()
    assert sched._scheduler.get_job("skill_security_scan") is not None
    await sched.stop()


async def test_start_registers_github_discovery(db):
    """A wired GitHubDiscoveryJob registers its weekly cron job on start().

    Locks the scheduler wiring: start() resolves CronTrigger / user_timezone and
    registers the job under id 'github_discovery'. A CronTrigger job does not
    enqueue surplus tasks, so sibling pending_count assertions are unaffected.
    """
    sched, compute = _make_scheduler(db, idle=True)
    sched.set_github_discovery_job(AsyncMock())
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.start()
    assert sched._scheduler.get_job("github_discovery") is not None
    await sched.stop()


async def test_schedule_code_audit_noop_when_disabled(db):
    sched, compute = _make_scheduler(db, idle=True, enable_code_audits=False)
    # Direct call should be a no-op
    await sched.schedule_code_audit()
    assert await sched._queue.pending_count() == 0


async def test_dispatch_drains_expired_tasks(db):
    sched, compute = _make_scheduler(db, idle=True)
    # Insert an old task directly
    old_time = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
    await db.execute(
        "INSERT INTO surplus_tasks (id, task_type, compute_tier, priority, drive_alignment, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("old-task-1", "brainstorm_self", "free_api", 0.5, "curiosity", "pending", old_time),
    )
    await db.commit()
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.dispatch_once()
    # Expired task should be drained
    cursor = await db.execute("SELECT COUNT(*) FROM surplus_tasks WHERE id = 'old-task-1' AND status = 'pending'")
    row = await cursor.fetchone()
    assert row[0] == 0


# ── DB integrity check (C1) ─────────────────────────────────────────────

async def test_alarm_db_integrity_writes_observation_and_emits(db):
    """Corruption detail is persisted as a critical observation AND emitted."""
    from genesis.observability.types import Severity, Subsystem

    sched, _ = _make_scheduler(db)
    sched._event_bus = AsyncMock()

    await sched._alarm_db_integrity("row 5 missing from index idx_x")

    cur = await db.execute(
        "SELECT priority, content FROM observations WHERE type = 'db_integrity_failure'"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["priority"] == "critical"
    assert "idx_x" in rows[0]["content"]

    assert sched._event_bus.emit.await_count == 1
    call = sched._event_bus.emit.await_args
    assert call.args[0] == Subsystem.SURPLUS
    assert call.args[1] == Severity.ERROR


async def test_run_db_integrity_check_healthy_db_no_alarm(db):
    """On a healthy DB the weekly job completes and raises no alarm."""
    sched, _ = _make_scheduler(db)
    sched._event_bus = AsyncMock()

    await sched.run_db_integrity_check()

    sched._event_bus.emit.assert_not_called()
    cur = await db.execute(
        "SELECT COUNT(*) FROM observations WHERE type = 'db_integrity_failure'"
    )
    assert (await cur.fetchone())[0] == 0


async def test_start_registers_db_integrity_check(db):
    """start() registers the deterministic weekly DB-integrity cron job.

    Locks the wiring: the job enqueues no surplus task (CronTrigger), so the
    pending_count assertions in the sibling start tests are unaffected.
    """
    sched, compute = _make_scheduler(db, idle=True)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.start()
    assert sched._scheduler.get_job("db_integrity_check") is not None
    await sched.stop()
