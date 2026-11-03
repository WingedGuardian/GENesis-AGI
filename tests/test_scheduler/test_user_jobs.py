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
        # The APScheduler should have 1 job registered
        jobs = scheduler._scheduler.get_jobs()
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
        assert len(scheduler._scheduler.get_jobs()) == 0

        # Resume
        ok = await scheduler.resume_job(job_id)
        assert ok
        job = await crud.get_job(db, job_id)
        assert job["status"] == "active"
        assert len(scheduler._scheduler.get_jobs()) == 1

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_add_and_remove(self, db) -> None:
        from genesis.scheduler.user_jobs import UserJobScheduler

        scheduler = UserJobScheduler(db=db)
        await scheduler.start()
        assert len(scheduler._scheduler.get_jobs()) == 0

        # Add a job
        job_id = await crud.create_job(
            db, title="New", cron_expression="0 0 * * 0",
            dispatch_prompt="weekly",
        )
        ok = await scheduler.add_job(job_id)
        assert ok
        assert len(scheduler._scheduler.get_jobs()) == 1

        # Remove it
        ok = await scheduler.remove_job(job_id)
        assert ok
        assert len(scheduler._scheduler.get_jobs()) == 0

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
        assert len(scheduler._scheduler.get_jobs()) == 0

        await scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("synthetic registration failure")])
async def test_create_retains_saved_id_when_registration_fails(db, monkeypatch, failure):
    from genesis.mcp.health import user_job_tools as tools
    from genesis.scheduler.user_jobs import UserJobScheduler

    scheduler = UserJobScheduler(db=db)
    await scheduler.start()
    monkeypatch.setattr(tools, "_db", db)
    monkeypatch.setattr(tools, "_scheduler", scheduler)
    original_add = scheduler.add_job

    async def register(_):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(scheduler, "add_job", register)
    try:
        result = await tools.user_job_create(
            title="Saved schedule", cron_expression="0 9 * * *", dispatch_prompt="test"
        )
        assert result["success"] is False
        assert result["saved"] is True and result["registered"] is False
        assert result["job_id"] in result["error"]
        saved = await crud.get_job(db, result["job_id"])
        assert saved["status"] == "active" and saved["dispatch_prompt"] == "test"
        listed = await tools.user_job_list()
        assert listed["jobs"][0]["registered"] is False
        monkeypatch.setattr(scheduler, "add_job", original_add)
        retried = await tools.user_job_control(result["job_id"], "resume")
        assert retried["success"] is True and retried["registered"] is True
        assert len(await crud.list_jobs(db)) == 1
    finally:
        await scheduler.stop()
    # Recreate the runtime wrapper: no duplicate definition, startup reloads
    # the retained active intent rather than trusting the old response.
    restarted = UserJobScheduler(db=db)
    await restarted.start()
    try:
        assert restarted.is_registered(result["job_id"])
        assert len(restarted._scheduler.get_jobs()) == 1
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_resume_reports_failed_attempt_without_erasing_old_registration(db, monkeypatch):
    from genesis.mcp.health import user_job_tools as tools
    from genesis.scheduler.user_jobs import UserJobScheduler

    job_id = await crud.create_job(db, title="Retained", cron_expression="0 9 * * *", dispatch_prompt="test")
    scheduler = UserJobScheduler(db=db)
    await scheduler.start()
    monkeypatch.setattr(tools, "_scheduler", scheduler)
    monkeypatch.setattr(scheduler, "_register_job", lambda _: False)
    try:
        result = await tools.user_job_control(job_id, "resume")
        assert result["success"] is False
        assert result["registered"] is True
        assert scheduler._scheduler.get_job(f"user_job:{job_id}") is not None
        assert (await crud.get_job(db, job_id))["status"] == "active"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "disabled"])
async def test_add_refuses_nonactive_definitions(db, state):
    from genesis.scheduler.user_jobs import UserJobScheduler

    job_id = await crud.create_job(db, title="Inactive", cron_expression="0 9 * * *", dispatch_prompt="test")
    await crud.update_job(db, job_id, status=state)
    scheduler = UserJobScheduler(db=db)
    await scheduler.start()
    try:
        assert await scheduler.add_job(job_id) is False
        assert scheduler._scheduler.get_jobs() == []
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_public_add_refuses_unstarted_scheduler(db):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from genesis.scheduler.user_jobs import UserJobScheduler

    job_id = await crud.create_job(db, title="Stopped", cron_expression="0 9 * * *", dispatch_prompt="test")
    scheduler = UserJobScheduler(db=db)
    scheduler._scheduler = AsyncIOScheduler()
    assert await scheduler.add_job(job_id) is False
    assert scheduler._scheduler.get_jobs() == []


@pytest.mark.asyncio
async def test_registration_membership_is_current_and_not_persisted(db, monkeypatch):
    from genesis.mcp.health import user_job_tools as tools
    from genesis.scheduler.user_jobs import UserJobScheduler

    job_id = await crud.create_job(db, title="Current", cron_expression="0 9 * * *", dispatch_prompt="test")
    scheduler = UserJobScheduler(db=db)
    await scheduler.start()
    monkeypatch.setattr(tools, "_db", db)
    monkeypatch.setattr(tools, "_scheduler", scheduler)
    try:
        assert scheduler.is_registered(job_id)
        assert (await tools.user_job_list())["jobs"][0]["registered"] is True
    finally:
        await scheduler.stop()
    assert not scheduler.is_registered(job_id)
    assert (await tools.user_job_list())["jobs"][0]["registered"] is False
    assert (await crud.get_job(db, job_id))["status"] == "active"


@pytest.mark.asyncio
async def test_resume_pending_definition_reports_registration_failure(db, monkeypatch):
    from genesis.scheduler.user_jobs import UserJobScheduler

    job_id = await crud.create_job(db, title="Pending", cron_expression="0 9 * * *", dispatch_prompt="test")
    scheduler = UserJobScheduler(db=db)
    await scheduler.start()
    await scheduler.pause_job(job_id)
    monkeypatch.setattr(scheduler, "_register_job", lambda _: False)
    try:
        assert await scheduler.resume_job(job_id) is False
        assert scheduler._scheduler.get_jobs() == []
        assert (await crud.get_job(db, job_id))["status"] == "active"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_dashboard_reports_registration_and_failed_resume(db, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from flask import Flask

    from genesis.dashboard.routes import scheduler as routes
    from genesis.runtime import GenesisRuntime
    from genesis.scheduler.user_jobs import UserJobScheduler

    job_id = await crud.create_job(db, title="Visible", cron_expression="0 9 * * *", dispatch_prompt="test")
    scheduler = UserJobScheduler(db=db)
    await scheduler.start()
    await scheduler.pause_job(job_id)
    monkeypatch.setattr(scheduler, "_register_job", lambda _: False)
    runtime = SimpleNamespace(is_bootstrapped=True, db=db, _user_job_scheduler=scheduler)
    monkeypatch.setattr(GenesisRuntime, "instance", lambda: runtime)
    app = Flask(__name__)
    app.config["GENESIS_EVENT_LOOP"] = asyncio.get_running_loop()
    # Only these existing handlers: registering the shared global blueprint
    # before dashboard.api loads would prevent its later page registration.
    app.add_url_rule("/api/genesis/scheduler/user", view_func=routes.user_scheduler_jobs)
    app.add_url_rule(
        "/api/genesis/scheduler/user/<job_id>/control",
        view_func=routes.user_job_control_endpoint, methods=["POST"],
    )

    def request(method, path, **kwargs):
        with app.test_client() as client:
            return getattr(client, method)(path, **kwargs).get_json()

    try:
        response = await asyncio.to_thread(
            request, "post", f"/api/genesis/scheduler/user/{job_id}/control", json={"action": "resume"}
        )
        assert response["success"] is False
        assert response["job_id"] == job_id
        assert response["registered"] is False
        assert response["error"]
        listed = await asyncio.to_thread(request, "get", "/api/genesis/scheduler/user")
        assert listed["jobs"][0]["status"] == "active"
        assert listed["jobs"][0]["registered"] is False
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_add_checks_running_after_lookup(db, monkeypatch):
    from genesis.scheduler.user_jobs import UserJobScheduler

    job_id = await crud.create_job(db, title="Stop race", cron_expression="0 9 * * *", dispatch_prompt="test")
    scheduler = UserJobScheduler(db=db)
    await scheduler.start()
    get_job = crud.get_job

    async def stop_during_lookup(*args):
        job = await get_job(*args)
        await scheduler.stop()
        return job

    monkeypatch.setattr(crud, "get_job", stop_during_lookup)
    assert await scheduler.add_job(job_id) is False
    assert not scheduler.is_registered(job_id)


@pytest.mark.asyncio
async def test_start_counts_only_actual_registrations(db, caplog):
    import logging

    from genesis.scheduler.user_jobs import UserJobScheduler

    await crud.create_job(db, title="Good", cron_expression="0 9 * * *", dispatch_prompt="test")
    await crud.create_job(db, title="Bad", cron_expression="invalid", dispatch_prompt="test")
    scheduler = UserJobScheduler(db=db)
    with caplog.at_level(logging.INFO, logger="genesis.scheduler.user_jobs"):
        await scheduler.start()
    try:
        assert "started with 1 registered, 1 pending job(s)" in caplog.text
        assert len(await crud.list_jobs(db, status="active")) == 2
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_failed_replacement_recovery_stays_single_after_restart(db, monkeypatch):
    from genesis.mcp.health import user_job_tools as tools
    from genesis.scheduler.user_jobs import UserJobScheduler

    old_id = await crud.create_job(db, title="Original", cron_expression="0 9 * * *", dispatch_prompt="old")
    scheduler = UserJobScheduler(db=db)
    await scheduler.start()
    monkeypatch.setattr(tools, "_db", db)
    monkeypatch.setattr(tools, "_scheduler", scheduler)
    try:
        assert (await tools.user_job_control(old_id, "pause"))["success"]
        register = scheduler._register_job
        monkeypatch.setattr(scheduler, "_register_job", lambda _: False)
        replacement = await tools.user_job_create(
            title="Replacement", cron_expression="0 10 * * *", dispatch_prompt="new"
        )
        assert replacement["success"] is False and replacement["saved"] is True
        # Retire the failed replacement's active intent BEFORE restoring old.
        assert (await tools.user_job_control(replacement["job_id"], "pause"))["success"]
        assert (await crud.get_job(db, replacement["job_id"]))["status"] == "paused"
        assert not scheduler.is_registered(replacement["job_id"])
        # A failed restore remains a failure, and both definitions survive.
        assert (await tools.user_job_control(old_id, "resume"))["success"] is False
        monkeypatch.setattr(scheduler, "_register_job", register)
        assert (await tools.user_job_control(old_id, "resume"))["success"] is True
    finally:
        await scheduler.stop()
    restarted = UserJobScheduler(db=db)
    await restarted.start()
    try:
        assert len(await crud.list_jobs(db)) == 2
        assert len(await crud.list_jobs(db, status="active")) == 1
        assert [j.id for j in restarted._scheduler.get_jobs()] == [f"user_job:{old_id}"]
    finally:
        await restarted.stop()
