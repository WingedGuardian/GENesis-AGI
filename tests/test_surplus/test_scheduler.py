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


def _make_scheduler(db, *, idle=True, lmstudio_up=False):
    idle_detector = IdleDetector()
    if idle:
        idle_detector._last_activity_at = datetime.now(UTC) - timedelta(minutes=30)
    else:
        idle_detector.mark_active()
    compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
    return SurplusScheduler(
        db=db,
        queue=SurplusQueue(db),
        idle_detector=idle_detector,
        compute_availability=compute,
        executor=StubExecutor(),
    ), compute


async def test_restart_safe_hourly_returns_crontrigger_not_interval():
    """Every sub-daily / daily cadence must come back as a CronTrigger (never Interval)."""
    for hours in (4, 6, 12, 24):
        trig = _restart_safe_hourly(hours)
        assert isinstance(trig, CronTrigger)
        assert not isinstance(trig, IntervalTrigger)


async def test_restart_safe_hourly_subdaily_is_stepped_and_daily_differs():
    """4h -> every-4-hours step; >=24h -> a single daily fire (the _recently_completed
    cooldown is the real cadence gate). Assert on trigger fields, not the __str__ repr."""

    def _field(trig, name):
        return next(str(f) for f in trig.fields if f.name == name)

    sub = _restart_safe_hourly(4, minute=10)
    daily = _restart_safe_hourly(24, minute=20)
    assert _field(sub, "hour") == "*/4"
    assert _field(sub, "minute") == "10"
    # >=24h collapses to a single fixed daily hour, not an every-24h step
    assert _field(daily, "hour") == "4"
    assert _field(daily, "minute") == "20"


async def test_long_interval_jobs_use_restart_safe_crontriggers(db):
    """The >1h jobs — schedule_maintenance, schedule_code_index — must
    use CronTrigger, not IntervalTrigger: a >1h IntervalTrigger resets on every restart and
    starves the job (the CLAUDE.md trap). Enumerated as a class, not just the flagged job."""
    sched, _ = _make_scheduler(db)
    await sched.start()
    try:
        for job_id in ("schedule_maintenance", "schedule_code_index"):
            job = sched._scheduler.get_job(job_id)
            assert job is not None, f"{job_id} not registered"
            assert isinstance(job.trigger, CronTrigger), f"{job_id} must be CronTrigger"
            assert not isinstance(job.trigger, IntervalTrigger)
    finally:
        sched._scheduler.shutdown(wait=False)


async def test_run_gitnexus_strip_cleans_both_files(db, tmp_path, monkeypatch):
    """run_gitnexus_strip removes marker blocks from CLAUDE.md AND AGENTS.md
    (safety net behind .gitnexusrc; AGENTS.md is hand-curated markerless, so
    only a re-injected block is ever removed)."""
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
    agents = (genesis_dir / "AGENTS.md").read_text()
    assert "gitnexus:start" not in agents, "block stripped from AGENTS.md too"
    assert "# Agents" in agents, "curated content preserved"


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
        db,
        id="ancient",
        task_type="brainstorm_self",
        compute_tier="free_api",
        priority=0.5,
        drive_alignment="curiosity",
        created_at=old,
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
    cur = await db.execute("SELECT outcome_quality FROM surplus_tasks WHERE status='completed'")
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
        findings_count=1,
        routed_knowledge=1,
        routed_observation=0,
        routed_discard=0,
    )
    judge = AsyncMock(return_value=judge_return)
    with (
        patch.object(
            compute,
            "_ping_lmstudio",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "genesis.surplus.intake.run_intake",
            new_callable=AsyncMock,
            return_value=benign,
        ),
        patch(
            "genesis.surplus.quality_judge.run_quality_judge",
            judge,
        ),
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
        db,
        TaskType.BRAINSTORM_USER,
        ("useful", 0.82, detail),
    )
    assert row["outcome_quality"] == "useful"
    assert row["judge_score"] == 0.82
    assert row["judge_detail"] == detail
    judge.assert_awaited_once()


async def test_dispatch_records_hollow_when_judge_fails(db):
    row, _ = await _dispatch_with_judge(
        db,
        TaskType.CODE_AUDIT,
        ("hollow", 0.30, '{"judge_score": 0.30}'),
    )
    assert row["outcome_quality"] == "hollow"
    assert row["judge_score"] == 0.30


async def test_dispatch_judge_outage_stays_null(db):
    # Judge unavailable → (None, None, None) → positive-only, no false hollow.
    row, _ = await _dispatch_with_judge(
        db,
        TaskType.WING_AUDIT,
        (None, None, None),
    )
    assert row["outcome_quality"] is None
    assert row["judge_score"] is None
    assert row["judge_detail"] is None


async def test_code_audit_skips_generic_intake(db):
    """CODE_AUDIT output is ingested per-finding by FindingsBridge (behind its
    confidence gate and slop filter); the generic intake route must not
    double-ingest the raw findings array or bypass those gates."""
    from genesis.surplus.intake import IntakeStats

    sched, compute = _make_scheduler(db, idle=True)
    await sched._queue.enqueue(
        TaskType.CODE_AUDIT,
        ComputeTier.FREE_API,
        0.8,
        "curiosity",
    )
    intake_mock = AsyncMock(return_value=IntakeStats(findings_count=1))
    with (
        patch.object(
            compute,
            "_ping_lmstudio",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "genesis.surplus.intake.run_intake",
            intake_mock,
        ),
        patch(
            "genesis.surplus.quality_judge.run_quality_judge",
            new_callable=AsyncMock,
            return_value=(None, None, None),
        ),
    ):
        assert await sched.dispatch_once() is True
    intake_mock.assert_not_awaited()
    cur = await db.execute(
        "SELECT status, result_staging_id FROM surplus_tasks WHERE status='completed'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["result_staging_id"]  # synthetic tracking id still recorded


async def test_dispatch_non_insight_type_skips_judge(db):
    # An action / non-insight type must NOT invoke the judge (cost) and stays NULL.
    row, judge = await _dispatch_with_judge(
        db,
        TaskType.CODE_INDEX,
        ("hollow", 0.1, "{}"),
    )
    assert row["outcome_quality"] is None
    judge.assert_not_awaited()


async def test_dispatch_pipeline_intermediate_skips_judge(db):
    # Pipeline-intermediate output feeds the next step, not the KB; excluded.
    row, judge = await _dispatch_with_judge(
        db,
        TaskType.RESEARCH_QUERY_GEN,
        ("hollow", 0.1, "{}"),
    )
    assert row["outcome_quality"] is None
    judge.assert_not_awaited()


async def test_dispatch_short_content_skips_judge(db):
    # Output shorter than the 50-char quality gate must NOT invoke the judge —
    # the verdict stays NULL (too little to grade), never hollow.
    from genesis.surplus.types import ExecutorResult

    sched, compute = _make_scheduler(db, idle=True)
    await sched._queue.enqueue(
        TaskType.BRAINSTORM_USER,
        ComputeTier.FREE_API,
        0.8,
        "curiosity",
    )

    async def _short(task):
        return ExecutorResult(
            success=True,
            content="too short",
            insights=[{"generating_model": "x"}],
        )

    sched._executor.execute = _short
    judge = AsyncMock(return_value=("hollow", 0.1, "{}"))
    with (
        patch.object(
            compute,
            "_ping_lmstudio",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("genesis.surplus.quality_judge.run_quality_judge", judge),
    ):
        await sched.dispatch_once()
    judge.assert_not_awaited()
    assert await _completed_verdict(db) is None


async def test_dispatch_empty_insights_stays_null(db):
    # NOMINAL / clean-pass (executor returns no insights) is NOT hollow — a
    # monitoring/scan type reporting "nothing noteworthy" is a valid success.
    from genesis.surplus.types import ExecutorResult

    sched, compute = _make_scheduler(db, idle=True)
    await sched._queue.enqueue(
        TaskType.BRAINSTORM_SELF,
        ComputeTier.FREE_API,
        0.8,
        "curiosity",
    )

    async def _nominal(task):
        return ExecutorResult(
            success=True,
            content="All clear — nothing noteworthy to capture in this pass today.",
            insights=[],
        )

    sched._executor.execute = _nominal
    with patch.object(
        compute,
        "_ping_lmstudio",
        new_callable=AsyncMock,
        return_value=False,
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
    sched, compute = _make_scheduler(db, idle=True)
    with (
        patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False),
        patch.object(sched, "run_gitnexus_strip", new_callable=AsyncMock),
    ):
        await sched.start()
    assert sched._scheduler.running is True
    # Verify jobs were registered
    assert sched._scheduler.get_job("surplus_brainstorm_check") is not None
    assert sched._scheduler.get_job("surplus_dispatch") is not None
    # Brainstorm (3) + code index (1) + model_eval (1)
    # + maintenance (4) + j9_eval_batch (1) + analytical (1: gap_clustering)
    # + pipeline (2: prompt_effectiveness + anticipatory_research step 1) = 13
    assert await sched._queue.pending_count() == 13
    # Stop should not raise
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
    cursor = await db.execute(
        "SELECT COUNT(*) FROM surplus_tasks WHERE id = 'old-task-1' AND status = 'pending'"
    )
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
    cur = await db.execute("SELECT COUNT(*) FROM observations WHERE type = 'db_integrity_failure'")
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


# ── run_dream_synthesis_drain wrapper ──────────────────────────────────────


def _fake_runtime(db):
    """Runtime stand-in for the drain wrapper: healthy deps, no flags set."""
    from unittest.mock import MagicMock

    rt = MagicMock()
    rt.paused = False
    rt.heavy_workload = None  # MagicMock auto-attrs are truthy — must be explicit
    rt._heavy_workload = None
    rt._heavy_workload_since = None
    rt.db = db
    rt.router = AsyncMock()
    rt.memory_store = MagicMock()
    rt.memory_store.qdrant_client = MagicMock()
    return rt


_DRAIN_REPORT = {
    "run_id": "t",
    "dry_run": True,
    "drained": 2,
    "would_merge": 2,
    "stale_skipped": 0,
    "errors": [],
}
_RT_CLS = "genesis.runtime.GenesisRuntime"
_DRAIN_FN = "genesis.memory.dream_cycle.run_synthesis_drain"


async def test_drain_wrapper_happy_path_shadow(db):
    """Wrapper drives the drain in SHADOW, records job health, writes the
    observation, and clears its own heavy_workload flag."""
    sched, _ = _make_scheduler(db)
    rt = _fake_runtime(db)
    with (
        patch(_RT_CLS) as rt_cls,
        patch(_DRAIN_FN, new_callable=AsyncMock, return_value=dict(_DRAIN_REPORT)) as drain,
    ):
        rt_cls.instance.return_value = rt
        await sched.run_dream_synthesis_drain()

    drain.assert_awaited_once()
    assert drain.call_args.kwargs["dry_run"] is True  # SHADOW hardwired in PR1
    rt.record_job_start.assert_called_once_with("dream_synthesis_drain")
    rt.record_job_success.assert_called_once_with("dream_synthesis_drain")
    rt.record_job_failure.assert_not_called()
    assert rt._heavy_workload is None  # cleared by the owner-guarded finally
    cur = await db.execute(
        "SELECT content FROM observations WHERE type = 'dream_synthesis_drain_report'"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert "SHADOW" in rows[0]["content"]
    assert "2 would merge" in rows[0]["content"]


async def test_drain_wrapper_paused_skips(db):
    sched, _ = _make_scheduler(db)
    rt = _fake_runtime(db)
    rt.paused = True
    with patch(_RT_CLS) as rt_cls, patch(_DRAIN_FN, new_callable=AsyncMock) as drain:
        rt_cls.instance.return_value = rt
        await sched.run_dream_synthesis_drain()
    drain.assert_not_awaited()
    rt.record_job_start.assert_not_called()


async def test_drain_wrapper_heavy_workload_skips(db):
    """Weekly clustering still running -> the drain defers to tomorrow (F6)."""
    sched, _ = _make_scheduler(db)
    rt = _fake_runtime(db)
    rt.heavy_workload = "dream_cycle"
    rt._heavy_workload = "dream_cycle"
    with patch(_RT_CLS) as rt_cls, patch(_DRAIN_FN, new_callable=AsyncMock) as drain:
        rt_cls.instance.return_value = rt
        await sched.run_dream_synthesis_drain()
    drain.assert_not_awaited()
    rt.record_job_start.assert_not_called()
    # the skip path must not touch the other job's flag
    assert rt._heavy_workload == "dream_cycle"


async def test_drain_wrapper_missing_deps_skip_before_job_start(db):
    """Dependency checks precede record_job_start — a misconfigured deploy
    must not leave the job perpetually 'started' in job_health."""
    sched, _ = _make_scheduler(db)
    rt = _fake_runtime(db)
    rt.db = None
    with patch(_RT_CLS) as rt_cls, patch(_DRAIN_FN, new_callable=AsyncMock) as drain:
        rt_cls.instance.return_value = rt
        await sched.run_dream_synthesis_drain()
    drain.assert_not_awaited()
    rt.record_job_start.assert_not_called()


async def test_drain_wrapper_failure_records_and_clears_flag(db):
    sched, _ = _make_scheduler(db)
    rt = _fake_runtime(db)
    with (
        patch(_RT_CLS) as rt_cls,
        patch(_DRAIN_FN, new_callable=AsyncMock, side_effect=RuntimeError("boom")),
    ):
        rt_cls.instance.return_value = rt
        await sched.run_dream_synthesis_drain()
    rt.record_job_failure.assert_called_once()
    assert rt.record_job_failure.call_args.args[0] == "dream_synthesis_drain"
    assert rt._heavy_workload is None


async def test_drain_wrapper_finally_is_owner_guarded(db):
    """If another job's flag replaced ours mid-run, the finally must NOT
    clear it — the two dream jobs share rt._heavy_workload."""
    sched, _ = _make_scheduler(db)
    rt = _fake_runtime(db)

    async def _drain_then_flag_stolen(**kw):
        rt._heavy_workload = "dream_cycle"  # weekly job took the flag
        return dict(_DRAIN_REPORT)

    with (
        patch(_RT_CLS) as rt_cls,
        patch(_DRAIN_FN, new_callable=AsyncMock, side_effect=_drain_then_flag_stolen),
    ):
        rt_cls.instance.return_value = rt
        await sched.run_dream_synthesis_drain()
    assert rt._heavy_workload == "dream_cycle"  # not clobbered


async def test_start_reclaims_orphaned_running_tasks_immediately(db):
    """Boot sweep (D1): a 'running' row left by the previous process is reset
    to 'pending' at start() regardless of age, without burning attempt_count.
    (Single-worker: nothing can legitimately be running before start().)"""
    recent = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await surplus_tasks.create(
        db,
        id="st-orphan",
        task_type="brainstorm",
        compute_tier="slm",
        priority=0.5,
        drive_alignment="curiosity",
        created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.mark_running(db, "st-orphan", started_at=recent)

    sched, compute = _make_scheduler(db, idle=True)
    with (
        patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False),
        patch.object(sched, "run_gitnexus_strip", new_callable=AsyncMock),
    ):
        await sched.start()
    try:
        row = await surplus_tasks.get_by_id(db, "st-orphan")
        assert row["status"] == "pending"
        assert row["attempt_count"] == 0
    finally:
        await sched.stop()
