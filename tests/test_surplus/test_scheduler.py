"""Tests for SurplusScheduler — dispatch loop + brainstorm orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.executor import StubExecutor
from genesis.surplus.idle_detector import IdleDetector
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.scheduler import SurplusScheduler
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
