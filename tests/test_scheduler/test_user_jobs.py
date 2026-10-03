"""Tests for user job registry — CRUD + scheduler."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import user_jobs as crud


@pytest.fixture()
async def db():
    """In-memory SQLite DB with user_jobs + user_job_runs tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("""
        CREATE TABLE user_jobs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            cron_expression TEXT NOT NULL,
            job_type TEXT NOT NULL DEFAULT 'generic',
            config_json TEXT,
            dispatch_prompt TEXT NOT NULL,
            profile TEXT NOT NULL DEFAULT 'observe',
            model TEXT NOT NULL DEFAULT 'sonnet',
            effort TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'active',
            last_run_at TEXT,
            last_status TEXT,
            last_result_json TEXT,
            next_run_at TEXT,
            run_requested_at TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.execute("""
        CREATE TABLE user_job_runs (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            session_id TEXT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            result_json TEXT,
            error_message TEXT
        )
    """)
    await conn.commit()
    yield conn
    await conn.close()


def _user_jobs(scheduler) -> list:
    """Only the real user jobs — not the scheduler's own reconcile tick."""
    return [
        j
        for j in scheduler._scheduler.get_jobs()
        if j.id != "user_job:__reconcile__"
    ]


class TestCRUD:
    """Test CRUD operations for user_jobs."""

    @pytest.mark.asyncio
    async def test_create_and_get(self, db) -> None:
        job_id = await crud.create_job(
            db,
            title="Test Job",
            cron_expression="0 9 * * *",
            dispatch_prompt="echo hello",
        )
        assert job_id  # UUID string

        job = await crud.get_job(db, job_id)
        assert job is not None
        assert job["title"] == "Test Job"
        assert job["cron_expression"] == "0 9 * * *"
        assert job["status"] == "active"
        assert job["profile"] == "observe"
        assert job["model"] == "sonnet"
        assert job["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_list_jobs(self, db) -> None:
        await crud.create_job(
            db, title="Job 1", cron_expression="0 1 * * *",
            dispatch_prompt="p1",
        )
        job2_id = await crud.create_job(
            db, title="Job 2", cron_expression="0 2 * * *",
            dispatch_prompt="p2",
        )
        await crud.update_job(db, job2_id, status="paused")

        # List all
        all_jobs = await crud.list_jobs(db)
        assert len(all_jobs) == 2

        # Filter by status
        active = await crud.list_jobs(db, status="active")
        assert len(active) == 1
        assert active[0]["title"] == "Job 1"

    @pytest.mark.asyncio
    async def test_update_job(self, db) -> None:
        job_id = await crud.create_job(
            db, title="Updatable", cron_expression="0 0 * * *",
            dispatch_prompt="run",
        )

        ok = await crud.update_job(db, job_id, status="paused")
        assert ok is True

        job = await crud.get_job(db, job_id)
        assert job["status"] == "paused"

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_false(self, db) -> None:
        ok = await crud.update_job(db, "nonexistent-id", status="paused")
        assert ok is False

    @pytest.mark.asyncio
    async def test_delete_job(self, db) -> None:
        job_id = await crud.create_job(
            db, title="Deletable", cron_expression="0 0 * * *",
            dispatch_prompt="run",
        )
        ok = await crud.delete_job(db, job_id)
        assert ok is True

        job = await crud.get_job(db, job_id)
        assert job is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, db) -> None:
        ok = await crud.delete_job(db, "nonexistent-id")
        assert ok is False


class TestRunHistory:
    """Test run history tracking."""

    @pytest.mark.asyncio
    async def test_record_run_lifecycle(self, db) -> None:
        job_id = await crud.create_job(
            db, title="Runner", cron_expression="0 0 * * *",
            dispatch_prompt="run",
        )

        # Start a run
        run_id = await crud.record_run_start(db, job_id=job_id)
        assert run_id

        # Job should show running
        job = await crud.get_job(db, job_id)
        assert job["last_status"] == "running"

        # Complete the run
        await crud.record_run_complete(
            db, run_id, status="passed",
            result_json={"output": "done"},
        )

        # Job should show passed with reset failure count
        job = await crud.get_job(db, job_id)
        assert job["last_status"] == "passed"
        assert job["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_failure_increments_count(self, db) -> None:
        job_id = await crud.create_job(
            db, title="Failer", cron_expression="0 0 * * *",
            dispatch_prompt="fail",
        )

        run_id = await crud.record_run_start(db, job_id=job_id)
        await crud.record_run_complete(
            db, run_id, status="failed",
            error_message="boom",
        )

        job = await crud.get_job(db, job_id)
        assert job["failure_count"] == 1
        assert job["last_status"] == "failed"

    @pytest.mark.asyncio
    async def test_get_run_history(self, db) -> None:
        job_id = await crud.create_job(
            db, title="History", cron_expression="0 0 * * *",
            dispatch_prompt="hist",
        )

        # Create 3 runs
        for _ in range(3):
            run_id = await crud.record_run_start(db, job_id=job_id)
            await crud.record_run_complete(db, run_id, status="passed")

        runs = await crud.get_run_history(db, job_id, limit=2)
        assert len(runs) == 2


class TestUserJobScheduler:
    """Test the scheduler class."""

    @pytest.mark.asyncio
    async def test_start_registers_active_jobs(self, db) -> None:
        await crud.create_job(
            db, title="Active", cron_expression="0 9 * * *",
            dispatch_prompt="run",
        )

        from genesis.scheduler.user_jobs import UserJobScheduler

        scheduler = UserJobScheduler(db=db)
        await scheduler.start()

        assert scheduler.is_running
        # The APScheduler should have 1 user job registered (the
        # scheduler's own reconcile tick also exists but is not a job).
        jobs = _user_jobs(scheduler)
        assert len(jobs) == 1
        assert jobs[0].id.startswith("user_job:")

        await scheduler.stop()
        assert not scheduler.is_running

    @pytest.mark.asyncio
    async def test_pause_and_resume(self, db) -> None:
        job_id = await crud.create_job(
            db, title="Pausable", cron_expression="0 9 * * *",
            dispatch_prompt="run",
        )

        from genesis.scheduler.user_jobs import UserJobScheduler

        scheduler = UserJobScheduler(db=db)
        await scheduler.start()

        # Pause
        ok = await scheduler.pause_job(job_id)
        assert ok
        job = await crud.get_job(db, job_id)
        assert job["status"] == "paused"
        assert len(_user_jobs(scheduler)) == 0

        # Resume
        ok = await scheduler.resume_job(job_id)
        assert ok
        job = await crud.get_job(db, job_id)
        assert job["status"] == "active"
        assert len(_user_jobs(scheduler)) == 1

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_add_and_remove(self, db) -> None:
        from genesis.scheduler.user_jobs import UserJobScheduler

        scheduler = UserJobScheduler(db=db)
        await scheduler.start()
        assert len(_user_jobs(scheduler)) == 0

        # Add a job
        job_id = await crud.create_job(
            db, title="New", cron_expression="0 0 * * 0",
            dispatch_prompt="weekly",
        )
        ok = await scheduler.add_job(job_id)
        assert ok
        assert len(_user_jobs(scheduler)) == 1

        # Remove it
        ok = await scheduler.remove_job(job_id)
        assert ok
        assert len(_user_jobs(scheduler)) == 0

        # Job should be deleted from DB
        job = await crud.get_job(db, job_id)
        assert job is None

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_invalid_cron_does_not_crash(self, db) -> None:
        """A job with an invalid cron expression should fail gracefully."""
        await crud.create_job(
            db, title="Bad Cron", cron_expression="invalid-cron",
            dispatch_prompt="run",
        )

        from genesis.scheduler.user_jobs import UserJobScheduler

        scheduler = UserJobScheduler(db=db)
        await scheduler.start()

        # The invalid job should not be registered
        assert len(_user_jobs(scheduler)) == 0

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_reconcile_registers_a_db_created_job(self, db) -> None:
        """A job written by a scheduler-less process must be picked up."""
        from genesis.scheduler.user_jobs import UserJobScheduler

        scheduler = UserJobScheduler(db=db)
        await scheduler.start()
        assert len(_user_jobs(scheduler)) == 0

        # Written straight to the DB — what the standalone MCP server does.
        job_id = await crud.create_job(
            db, title="External", cron_expression="0 3 * * *",
            dispatch_prompt="run",
        )
        await scheduler._reconcile()
        assert [j.id for j in _user_jobs(scheduler)] == [f"user_job:{job_id}"]

        # A pause written from the other side removes it again.
        await crud.update_job(db, job_id, status="paused")
        await scheduler._reconcile()
        assert len(_user_jobs(scheduler)) == 0

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_reconcile_dispatches_a_requested_run(self, db, monkeypatch) -> None:
        """run_now written via the DB channel is consumed and cleared."""
        from genesis.scheduler.user_jobs import UserJobScheduler

        job_id = await crud.create_job(
            db, title="Manual", cron_expression="0 3 * * *",
            dispatch_prompt="run",
        )
        ok = await crud.request_run(db, job_id)
        assert ok

        scheduler = UserJobScheduler(db=db)
        await scheduler.start()

        dispatched: list[str] = []

        async def _fake_dispatch(jid):
            dispatched.append(jid)
            return "fake-session"

        monkeypatch.setattr(scheduler, "_dispatch_job", _fake_dispatch)
        await scheduler._reconcile()

        assert dispatched == [job_id]
        job = await crud.get_job(db, job_id)
        assert job["run_requested_at"] is None

        await scheduler.stop()
