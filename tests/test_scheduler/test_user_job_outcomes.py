"""User-job runs follow the outcome of their background session."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from genesis.db.crud import cc_sessions
from genesis.db.crud import user_jobs as crud
from genesis.scheduler.user_jobs import UserJobScheduler


async def _create_job(db) -> str:
    return await crud.create_job(
        db,
        title="Outcome test",
        cron_expression="0 9 * * *",
        dispatch_prompt="finish this task",
    )


async def _create_session(
    db,
    session_id: str,
    *,
    status: str = "active",
    outcome: str | None = None,
    outcome_reason: str | None = None,
    metadata: str | dict | None = None,
    last_activity_at: str | None = None,
    keep_outcome_null: bool = False,
    raw_outcome: bool = False,
) -> None:
    now = datetime.now(UTC).isoformat()
    activity = last_activity_at or now
    metadata_json = json.dumps(metadata) if isinstance(metadata, dict) else metadata
    await cc_sessions.create(
        db,
        id=session_id,
        session_type="background_task",
        model="sonnet",
        started_at=now,
        last_activity_at=activity,
        status="active",
        source_tag="user_job:test",
        metadata=metadata_json,
    )
    if status == "active":
        return
    if keep_outcome_null or raw_outcome:
        await db.execute(
            "UPDATE cc_sessions SET status = ?, completed_at = ?, outcome = ?, "
            "outcome_reason = ? WHERE id = ?",
            (status, now, None if keep_outcome_null else outcome, outcome_reason, session_id),
        )
        await db.commit()
    else:
        await cc_sessions.update_status(
            db,
            session_id,
            status=status,
            outcome=outcome,
            outcome_reason=outcome_reason,
        )


async def _create_run(db, job_id: str, session_id: str | None = None) -> str:
    run_id = await crud.record_run_start(db, job_id=job_id)
    if session_id is not None:
        assert await crud.attach_run_session(db, run_id, session_id)
    return run_id


def _runtime(monkeypatch, runner):
    from genesis.runtime import GenesisRuntime

    runtime = SimpleNamespace(
        _direct_session_runner=runner,
        record_job_success=Mock(),
        record_job_failure=Mock(),
    )
    monkeypatch.setattr(
        GenesisRuntime,
        "instance",
        classmethod(lambda cls: runtime),
    )
    return runtime


def _outcome_row(
    *,
    status: str,
    outcome: str | None,
    outcome_reason: str | None = None,
    metadata: str | dict | None = None,
) -> dict:
    metadata_value = json.dumps(metadata) if isinstance(metadata, dict) else metadata
    return {
        "id": "session-1",
        "status": status,
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "metadata": metadata_value,
    }


@pytest.mark.asyncio
async def test_dispatch_keeps_run_running_until_session_ends(db, monkeypatch):
    job_id = await _create_job(db)
    session_id = "dispatch-active"

    async def spawn(_request):
        await _create_session(db, session_id)
        return session_id

    runner = SimpleNamespace(spawn=AsyncMock(side_effect=spawn))
    runtime = _runtime(monkeypatch, runner)
    scheduler = UserJobScheduler(db=db)

    dispatched_id = await scheduler._dispatch_job(job_id)

    run = (await crud.get_run_history(db, job_id))[0]
    job = await crud.get_job(db, job_id)
    assert dispatched_id == session_id
    assert run["status"] == "running"
    assert run["session_id"] == session_id
    assert run["completed_at"] is None
    assert job["last_status"] == "running"
    runtime.record_job_success.assert_called_once_with(f"user_job:{job_id[:8]}")
    runtime.record_job_failure.assert_not_called()


@pytest.mark.parametrize(
    ("row", "expected_status", "expected_error", "expected_result"),
    [
        (
            None,
            "failed",
            "session record not found",
            {
                "session_id": None,
                "session_status": None,
                "session_outcome": None,
                "session_outcome_reason": None,
            },
        ),
        (_outcome_row(status="active", outcome="failed"), None, None, None),
        (_outcome_row(status="checkpointed", outcome="partial"), None, None, None),
        (_outcome_row(status="unknown-status", outcome="finished"), None, None, None),
        (_outcome_row(status="completed", outcome="finished"), "passed", None, None),
        (
            _outcome_row(status="completed", outcome="blocked"),
            "failed",
            "session reported blocked",
            None,
        ),
        (_outcome_row(status="completed", outcome="failed"), "failed", "session failed", None),
        (
            _outcome_row(status="completed", outcome="partial"),
            "failed",
            "session result incomplete (partial)",
            None,
        ),
        (
            _outcome_row(status="completed", outcome="unreported"),
            "failed",
            "session outcome unknown/unreported; execution failure is not confirmed",
            None,
        ),
        (
            _outcome_row(status="completed", outcome="expired"),
            "failed",
            "session expired before reporting a result; outcome unknown",
            None,
        ),
        (
            _outcome_row(status="completed", outcome="parked"),
            "failed",
            "session parked for rate limit",
            None,
        ),
        (
            _outcome_row(status="completed", outcome=None),
            "failed",
            "session outcome unknown/unreported; execution failure is not confirmed",
            None,
        ),
        (
            _outcome_row(status="completed", outcome="future-value"),
            "failed",
            "session outcome unknown; execution failure is not confirmed",
            None,
        ),
        (
            _outcome_row(status="failed", outcome="finished"),
            "failed",
            "session failed",
            None,
        ),
        (
            _outcome_row(status="expired", outcome="finished"),
            "failed",
            "session expired before reporting a result; outcome unknown",
            None,
        ),
    ],
)
def test_session_outcome_table(row, expected_status, expected_error, expected_result):
    from genesis.scheduler import user_jobs

    mapped = user_jobs.session_outcome(row)

    if expected_status is None:
        assert mapped is None
        return
    status, error, result = mapped
    assert status == expected_status
    assert error == expected_error
    if row is None:
        assert result == expected_result
    else:
        assert result == {
            "session_id": row["id"],
            "session_status": row["status"],
            "session_outcome": row["outcome"],
            "session_outcome_reason": row["outcome_reason"],
        }


@pytest.mark.parametrize(
    "metadata",
    [
        "not json",
        "[]",
        [],
        7,
        None,
        {"error": "", "park_id": "p1"},
        {"park_id": None},
        {"park_id": ""},
    ],
)
def test_session_outcome_parses_metadata_defensively(metadata):
    from genesis.scheduler import user_jobs

    mapped = user_jobs.session_outcome(
        _outcome_row(status="completed", outcome="finished", metadata=metadata),
    )

    assert mapped == (
        "passed",
        None,
        {
            "session_id": "session-1",
            "session_status": "completed",
            "session_outcome": "finished",
            "session_outcome_reason": None,
            **(
                {"park_id": metadata["park_id"]}
                if isinstance(metadata, dict) and "park_id" in metadata
                else {}
            ),
        },
    )


def test_session_outcome_error_precedence_and_truncation():
    from genesis.scheduler import user_jobs

    mapped = user_jobs.session_outcome(
        _outcome_row(
            status="completed",
            outcome="blocked",
            outcome_reason="model reason",
            metadata={"error": "mechanical error"},
        ),
    )
    assert mapped[1] == "mechanical error"

    reason_only = user_jobs.session_outcome(
        _outcome_row(status="completed", outcome="blocked", outcome_reason="model reason"),
    )
    assert reason_only[1] == "model reason"

    whitespace_error = user_jobs.session_outcome(
        _outcome_row(status="completed", outcome="finished", metadata={"error": "  "}),
    )
    assert whitespace_error[0:2] == ("failed", "  ")

    whitespace_reason = user_jobs.session_outcome(
        _outcome_row(status="completed", outcome="blocked", outcome_reason="  "),
    )
    assert whitespace_reason[1] == "  "

    long_error = user_jobs.session_outcome(
        _outcome_row(
            status="completed",
            outcome="failed",
            metadata={"error": "e" * 5000},
        ),
    )
    assert long_error[1] == "e" * 2000


def test_session_outcome_preserves_park_id_and_outcome_reason():
    from genesis.scheduler import user_jobs

    mapped = user_jobs.session_outcome(
        _outcome_row(
            status="completed",
            outcome="parked",
            outcome_reason="rate limit",
            metadata='{"park_id":"park-1"}',
        ),
    )

    assert mapped[2] == {
        "session_id": "session-1",
        "session_status": "completed",
        "session_outcome": "parked",
        "session_outcome_reason": "rate limit",
        "park_id": "park-1",
    }


@pytest.mark.asyncio
async def test_attach_and_lookup_running_run(db):
    job_id = await _create_job(db)
    run_id = await crud.record_run_start(db, job_id=job_id)

    assert await crud.attach_run_session(db, run_id, "session-1") is True
    assert await crud.attach_run_session(db, "missing-run", "session-2") is False
    assert (await crud.get_running_run_by_session(db, "session-1"))["id"] == run_id
    assert await crud.get_running_run_by_session(db, "missing-session") is None
    assert await crud.get_running_run_by_session(db, "") is None

    await crud.record_run_complete(db, run_id, status="passed")
    assert await crud.get_running_run_by_session(db, "session-1") is None
    assert await crud.attach_run_session(db, run_id, "late-session") is True
    assert (await crud.get_run_history(db, job_id))[0]["session_id"] == "late-session"


@pytest.mark.asyncio
async def test_list_running_runs_returns_all_in_start_order(db):
    job_id = await _create_job(db)
    run_ids = [await crud.record_run_start(db, job_id=job_id) for _ in range(3)]
    for index, run_id in enumerate(run_ids):
        await db.execute(
            "UPDATE user_job_runs SET started_at = ? WHERE id = ?",
            (f"2026-01-0{index + 1}", run_id),
        )
    await db.commit()
    await crud.record_run_complete(db, run_ids[1], status="passed")

    running = await crud.list_running_runs(db)

    assert [run["id"] for run in running] == [run_ids[0], run_ids[2]]
    assert all(run["status"] == "running" for run in running)


@pytest.mark.asyncio
async def test_record_run_complete_only_if_running_is_idempotent(db):
    job_id = await _create_job(db)
    run_id = await crud.record_run_start(db, job_id=job_id)

    assert await crud.record_run_complete(
        db, run_id, status="failed", error_message="boom", only_if_running=True,
    ) is True
    first = (await crud.get_run_history(db, job_id))[0]
    assert await crud.record_run_complete(
        db, run_id, status="failed", error_message="boom", only_if_running=True,
    ) is False
    second = (await crud.get_run_history(db, job_id))[0]
    job = await crud.get_job(db, job_id)

    assert second["completed_at"] == first["completed_at"]
    assert job["failure_count"] == 1


@pytest.mark.asyncio
async def test_record_run_complete_only_if_running_missing_row_skips_parent(db):
    job_id = await _create_job(db)
    run_id = await crud.record_run_start(db, job_id=job_id)
    await db.execute("DELETE FROM user_job_runs WHERE id = ?", (run_id,))
    await db.commit()

    assert await crud.record_run_complete(
        db, run_id, status="failed", only_if_running=True,
    ) is False
    assert (await crud.get_job(db, job_id))["failure_count"] == 0


@pytest.mark.asyncio
async def test_record_run_complete_default_remains_unconditional_and_returns_true(db):
    job_id = await _create_job(db)
    run_id = await crud.record_run_start(db, job_id=job_id)
    assert await crud.record_run_complete(
        db, run_id, status="passed", result_json={},
    ) is True
    run = (await crud.get_run_history(db, job_id))[0]
    job = await crud.get_job(db, job_id)
    assert run["result_json"] is None
    assert job["last_result_json"] is None
    assert await crud.record_run_complete(db, "missing-run", status="failed") is True

    assert await crud.record_run_complete(db, run_id, status="failed") is True
    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "failed"
    assert (await crud.get_job(db, job_id))["failure_count"] == 1


async def _two_overlapping_runs(db) -> tuple[str, str, str]:
    """A job with an older and a newer run, both still running."""
    job_id = await _create_job(db)
    older = await crud.record_run_start(db, job_id=job_id)
    newer = await crud.record_run_start(db, job_id=job_id)
    for run_id, started in ((older, "2026-01-01T09:00:00+00:00"),
                            (newer, "2026-01-01T09:01:00+00:00")):
        await db.execute(
            "UPDATE user_job_runs SET started_at = ? WHERE id = ?", (started, run_id),
        )
    await db.commit()
    return job_id, older, newer


@pytest.mark.asyncio
async def test_an_older_completion_keeps_the_newer_runs_summary(db):
    job_id, older, newer = await _two_overlapping_runs(db)

    assert await crud.record_run_complete(
        db, older, status="passed", result_json={"run": "older"}, only_if_running=True,
    ) is True
    job = await crud.get_job(db, job_id)
    assert job["last_status"] == "running"
    assert job["last_result_json"] is None

    assert await crud.record_run_complete(
        db, newer, status="passed", result_json={"run": "newer"}, only_if_running=True,
    ) is True
    job = await crud.get_job(db, job_id)
    assert job["last_status"] == "passed"
    assert json.loads(job["last_result_json"]) == {"run": "newer"}


@pytest.mark.asyncio
async def test_an_older_success_neither_hides_nor_resets_a_newer_failure(db):
    job_id, older, newer = await _two_overlapping_runs(db)

    await crud.record_run_complete(db, newer, status="failed", only_if_running=True)
    await crud.record_run_complete(db, older, status="passed", only_if_running=True)

    job = await crud.get_job(db, job_id)
    assert job["last_status"] == "failed"
    assert job["failure_count"] == 1


@pytest.mark.asyncio
async def test_an_older_failure_still_counts_without_touching_the_summary(db):
    job_id, older, newer = await _two_overlapping_runs(db)

    await crud.record_run_complete(db, older, status="failed", only_if_running=True)

    job = await crud.get_job(db, job_id)
    assert job["failure_count"] == 1
    assert job["last_status"] == "running"


@pytest.mark.asyncio
async def test_overlapping_failures_count_from_the_database_not_a_stale_read(db, monkeypatch):
    """Two completions that each read the job before either writes must both count."""
    job_id, older, newer = await _two_overlapping_runs(db)
    stale = dict(await crud.get_job(db, job_id))  # failure_count 0, as both readers saw it

    real_get_job = crud.get_job

    async def stale_get_job(_db, _job_id):
        return dict(stale)

    monkeypatch.setattr(crud, "get_job", stale_get_job)
    await crud.record_run_complete(db, older, status="failed", only_if_running=True)
    await crud.record_run_complete(db, newer, status="failed", only_if_running=True)

    assert (await real_get_job(db, job_id))["failure_count"] == 2


async def _fail_the_parent_update_once(db, monkeypatch) -> None:
    """Make the next parent-job UPDATE raise, as a failure between the two writes."""
    # Patch the raw connection under the fixture's serializing wrapper: the
    # wrapper holds its lock while it calls this, so the fake must not re-enter it.
    raw = db._conn
    real_execute = raw.execute
    calls = {"failed": False}

    async def failing_execute(sql, *args, **kwargs):
        if not calls["failed"] and sql.lstrip().startswith("UPDATE user_jobs"):
            calls["failed"] = True
            raise RuntimeError("parent update failed")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(raw, "execute", failing_execute)


async def _a_run_saved_without_its_parent(db, monkeypatch) -> tuple[str, str]:
    """A failed run whose completion saved the run row but not the parent."""
    job_id = await _create_job(db)
    run_id = await crud.record_run_start(db, job_id=job_id)
    await _fail_the_parent_update_once(db, monkeypatch)
    with pytest.raises(RuntimeError):
        await crud.record_run_complete(db, run_id, status="failed", only_if_running=True)
    await db.commit()  # any later commit on the shared connection saves the run row
    assert (await crud.get_run_history(db, job_id))[0]["status"] == "failed"
    job = await crud.get_job(db, job_id)
    assert (job["last_status"], job["failure_count"]) == ("running", 0)
    return job_id, run_id


@pytest.mark.asyncio
async def test_a_repeat_completion_repairs_a_parent_the_first_one_missed(db, monkeypatch):
    job_id, run_id = await _a_run_saved_without_its_parent(db, monkeypatch)

    assert await crud.record_run_complete(
        db, run_id, status="failed", only_if_running=True,
    ) is False  # the run was already finished
    job = await crud.get_job(db, job_id)
    assert (job["last_status"], job["failure_count"]) == ("failed", 1)


@pytest.mark.asyncio
async def test_boot_reconciliation_repairs_a_parent_left_marked_running(db, monkeypatch):
    job_id, _ = await _a_run_saved_without_its_parent(db, monkeypatch)

    await UserJobScheduler(db=db).reconcile_open_runs()

    job = await crud.get_job(db, job_id)
    assert (job["last_status"], job["failure_count"]) == ("failed", 1)


@pytest.mark.asyncio
async def test_boot_repair_leaves_a_job_failed_without_a_run_alone(db):
    """A dispatch that never started a run marks its job failed with no run row."""
    job_id = await _create_job(db)
    run_id = await crud.record_run_start(db, job_id=job_id)
    await crud.record_run_complete(db, run_id, status="passed")
    await crud.update_job(db, job_id, last_status="failed")

    await UserJobScheduler(db=db).reconcile_open_runs()

    assert (await crud.get_job(db, job_id))["last_status"] == "failed"


@pytest.mark.asyncio
async def test_dispatch_finalizes_session_that_ended_before_attachment(db, monkeypatch):
    job_id = await _create_job(db)
    session_id = "dispatch-fast-finish"

    async def spawn(_request):
        await _create_session(db, session_id, status="completed", outcome="finished")
        return session_id

    runtime = _runtime(monkeypatch, SimpleNamespace(spawn=AsyncMock(side_effect=spawn)))
    scheduler = UserJobScheduler(db=db)

    assert await scheduler._dispatch_job(job_id) == session_id
    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "passed"
    assert run["session_id"] == session_id
    assert json.loads(run["result_json"]) == {
        "session_id": session_id,
        "session_status": "completed",
        "session_outcome": "finished",
        "session_outcome_reason": None,
    }
    runtime.record_job_failure.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("attach_result", ["raise", "false"])
async def test_post_spawn_attach_failure_does_not_fail_dispatch(
    db, monkeypatch, attach_result,
):
    job_id = await _create_job(db)
    session_id = "dispatch-attach-failure"
    runner = SimpleNamespace(spawn=AsyncMock(return_value=session_id))
    runtime = _runtime(monkeypatch, runner)
    scheduler = UserJobScheduler(db=db)
    scheduler._finalize_run = AsyncMock()

    if attach_result == "raise":
        async def attach_error(*_args, **_kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(crud, "attach_run_session", attach_error)
    else:
        monkeypatch.setattr(crud, "attach_run_session", AsyncMock(return_value=False))

    assert await scheduler._dispatch_job(job_id) == session_id
    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "running"
    assert run["session_id"] is None
    scheduler._finalize_run.assert_not_awaited()
    runtime.record_job_success.assert_called_once()
    runtime.record_job_failure.assert_not_called()


@pytest.mark.asyncio
async def test_post_spawn_finalize_failure_does_not_fail_dispatch(db, monkeypatch):
    job_id = await _create_job(db)
    session_id = "dispatch-finalize-failure"
    runtime = _runtime(monkeypatch, SimpleNamespace(spawn=AsyncMock(return_value=session_id)))
    scheduler = UserJobScheduler(db=db)

    original_attach = crud.attach_run_session

    async def attach(db_arg, run_id, sid):
        return await original_attach(db_arg, run_id, sid)

    monkeypatch.setattr(crud, "attach_run_session", attach)
    scheduler._finalize_run = AsyncMock(side_effect=RuntimeError("session read failed"))

    assert await scheduler._dispatch_job(job_id) == session_id
    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "running"
    assert run["session_id"] == session_id
    runtime.record_job_success.assert_called_once()
    runtime.record_job_failure.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_failure_still_records_failed(db, monkeypatch):
    job_id = await _create_job(db)
    runtime = _runtime(
        monkeypatch,
        SimpleNamespace(spawn=AsyncMock(side_effect=RuntimeError("spawn failed"))),
    )
    scheduler = UserJobScheduler(db=db)

    assert await scheduler._dispatch_job(job_id) is None
    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "failed"
    assert run["error_message"] == "spawn failed"
    assert (await crud.get_job(db, job_id))["failure_count"] == 1
    runtime.record_job_failure.assert_called_once()
    runtime.record_job_success.assert_not_called()


@pytest.mark.asyncio
async def test_record_run_start_failure_still_fails_dispatch(db, monkeypatch):
    job_id = await _create_job(db)
    runtime = _runtime(monkeypatch, SimpleNamespace(spawn=AsyncMock()))

    async def start_error(*_args, **_kwargs):
        raise RuntimeError("run start failed")

    monkeypatch.setattr(crud, "record_run_start", start_error)

    assert await UserJobScheduler(db=db)._dispatch_job(job_id) is None
    runtime._direct_session_runner.spawn.assert_not_awaited()
    runtime.record_job_failure.assert_called_once()
    runtime.record_job_success.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "outcome_reason", "metadata", "expected_status", "expected_error"),
    [
        ("completed", "finished", None, None, "passed", None),
        ("completed", "blocked", None, None, "failed", "session reported blocked"),
        ("completed", "failed", None, None, "failed", "session failed"),
        (
            "completed", "partial", None, None, "failed", "session result incomplete (partial)"
        ),
        (
            "completed", "unreported", None, None, "failed",
            "session outcome unknown/unreported; execution failure is not confirmed",
        ),
        (
            "expired", "expired", "expired_idle", None, "failed",
            "expired_idle",
        ),
        (
            "completed", "parked", "rate_limited", {"park_id": "park-1"},
            "failed", "rate_limited",
        ),
    ],
)
async def test_end_hook_maps_terminal_outcomes(
    db, status, outcome, outcome_reason, metadata, expected_status, expected_error, caplog,
):
    job_id = await _create_job(db)
    session_id = f"end-{outcome}"
    run_id = await _create_run(db, job_id, session_id)
    await _create_session(
        db,
        session_id,
        status=status,
        outcome=outcome,
        outcome_reason=outcome_reason,
        metadata=metadata,
    )

    with caplog.at_level("INFO", logger="genesis.scheduler.user_jobs"):
        await UserJobScheduler(db=db).on_session_end(session_id)

    run = (await crud.get_run_history(db, job_id))[0]
    job = await crud.get_job(db, job_id)
    assert run["id"] == run_id
    assert run["status"] == expected_status
    assert run["error_message"] == expected_error
    assert job["last_status"] == expected_status
    assert job["failure_count"] == (0 if expected_status == "passed" else 1)
    result = json.loads(run["result_json"])
    assert result["session_id"] == session_id
    assert result["session_status"] == status
    assert result["session_outcome"] == outcome
    assert result["session_outcome_reason"] == outcome_reason
    assert f"run {run_id[:8]} session {session_id[:8]} as {expected_status}" in caplog.text
    if metadata and "park_id" in metadata:
        assert result["park_id"] == metadata["park_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "expected_error"),
    [
        (
            "completed", None,
            "session outcome unknown/unreported; execution failure is not confirmed",
        ),
        (
            "completed", "future-value",
            "session outcome unknown; execution failure is not confirmed",
        ),
        (
            "failed", "finished", "session failed",
        ),
        (
            "expired", "finished",
            "session expired before reporting a result; outcome unknown",
        ),
    ],
)
async def test_end_hook_handles_legacy_unknown_and_conflicting_outcomes(
    db, status, outcome, expected_error,
):
    job_id = await _create_job(db)
    session_id = f"edge-{status}-{outcome}"
    await _create_run(db, job_id, session_id)
    await _create_session(
        db,
        session_id,
        status=status,
        outcome=outcome,
        keep_outcome_null=outcome is None,
        raw_outcome=outcome == "future-value",
    )

    await UserJobScheduler(db=db).on_session_end(session_id)

    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "failed"
    assert run["error_message"] == expected_error
    result = json.loads(run["result_json"])
    assert result["session_outcome"] == outcome
    assert result["session_outcome_reason"] is None
    assert (await crud.get_job(db, job_id))["failure_count"] == 1


@pytest.mark.asyncio
async def test_end_hook_metadata_error_precedes_outcome_reason(db):
    job_id = await _create_job(db)
    session_id = "end-metadata-error"
    await _create_run(db, job_id, session_id)
    await _create_session(
        db,
        session_id,
        status="completed",
        outcome="finished",
        outcome_reason="self-reported reason",
        metadata={"error": "runner error"},
    )

    await UserJobScheduler(db=db).on_session_end(session_id)

    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "failed"
    assert run["error_message"] == "runner error"
    assert json.loads(run["result_json"])["session_outcome_reason"] == "self-reported reason"


@pytest.mark.asyncio
async def test_end_hook_uses_outcome_reason_before_fallback(db):
    job_id = await _create_job(db)
    session_id = "end-outcome-reason"
    await _create_run(db, job_id, session_id)
    await _create_session(
        db,
        session_id,
        status="completed",
        outcome="blocked",
        outcome_reason="tool access denied",
    )

    await UserJobScheduler(db=db).on_session_end(session_id)

    assert (await crud.get_run_history(db, job_id))[0]["error_message"] == "tool access denied"


@pytest.mark.asyncio
async def test_end_hook_missing_session_row_fails_run(db):
    job_id = await _create_job(db)
    await _create_run(db, job_id, "missing-session")

    await UserJobScheduler(db=db).on_session_end("missing-session")

    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "failed"
    assert run["error_message"] == "session record not found"
    assert json.loads(run["result_json"]) == {
        "session_id": "missing-session",
        "session_status": None,
        "session_outcome": None,
        "session_outcome_reason": None,
    }


@pytest.mark.asyncio
async def test_end_hook_ignores_unrelated_session_and_active_session(db):
    job_id = await _create_job(db)
    await _create_run(db, job_id, "active-session")
    await _create_session(db, "active-session", status="active")
    scheduler = UserJobScheduler(db=db)

    await scheduler.on_session_end("unrelated-session")
    await scheduler.on_session_end("active-session")

    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "running"
    assert (await crud.get_job(db, job_id))["failure_count"] == 0


@pytest.mark.asyncio
async def test_end_hook_never_raises(db, monkeypatch):
    async def broken_lookup(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(crud, "get_running_run_by_session", broken_lookup)
    await UserJobScheduler(db=db).on_session_end("session-1")


@pytest.mark.asyncio
async def test_hook_and_reconcile_cannot_double_count_failure(db):
    job_id = await _create_job(db)
    session_id = "idempotent-failure"
    await _create_run(db, job_id, session_id)
    await _create_session(db, session_id, status="completed", outcome="failed")
    scheduler = UserJobScheduler(db=db)

    await scheduler.on_session_end(session_id)
    assert await scheduler.reconcile_open_runs() == 0

    assert (await crud.get_job(db, job_id))["failure_count"] == 1


@pytest.mark.asyncio
async def test_reconcile_fails_run_without_attached_session(db, caplog):
    job_id = await _create_job(db)
    await crud.record_run_start(db, job_id=job_id)
    scheduler = UserJobScheduler(db=db)
    run = (await crud.get_run_history(db, job_id))[0]

    assert await scheduler._finalize_run(run) is False
    with caplog.at_level("INFO", logger="genesis.scheduler.user_jobs"):
        assert await scheduler.reconcile_open_runs() == 1
    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "failed"
    assert run["error_message"] == "dispatch interrupted before a session was attached"
    assert (await crud.get_job(db, job_id))["failure_count"] == 1
    assert f"run {run['id'][:8]} session none as failed" in caplog.text
    assert "Reconciled 1 user-job run(s)" in caplog.text


@pytest.mark.asyncio
async def test_reconcile_finalizes_terminal_but_leaves_active_session(db):
    job_id = await _create_job(db)
    done_id, active_id = "reconcile-done", "reconcile-active"
    await _create_run(db, job_id, done_id)
    await _create_run(db, job_id, active_id)
    await _create_session(db, done_id, status="completed", outcome="finished")
    await _create_session(db, active_id)

    assert await UserJobScheduler(db=db).reconcile_open_runs() == 1
    runs = {run["session_id"]: run for run in await crud.get_run_history(db, job_id)}
    assert runs[done_id]["status"] == "passed"
    assert runs[active_id]["status"] == "running"


@pytest.mark.asyncio
async def test_reconcile_fails_missing_session_row(db):
    job_id = await _create_job(db)
    await _create_run(db, job_id, "reconcile-missing")

    assert await UserJobScheduler(db=db).reconcile_open_runs() == 1
    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "failed"
    assert run["error_message"] == "session record not found"


@pytest.mark.asyncio
async def test_reconcile_isolates_a_bad_run_from_later_rows(db, monkeypatch):
    job_id = await _create_job(db)
    bad_id, good_id = "reconcile-bad", "reconcile-good"
    await _create_run(db, job_id, bad_id)
    await _create_run(db, job_id, good_id)
    await _create_session(db, good_id, status="completed", outcome="finished")
    original_get = cc_sessions.get_by_id

    async def selective_error(db_arg, session_id):
        if session_id == bad_id:
            raise RuntimeError("one corrupt session row")
        return await original_get(db_arg, session_id)

    monkeypatch.setattr(cc_sessions, "get_by_id", selective_error)

    assert await UserJobScheduler(db=db).reconcile_open_runs() == 1
    runs = {run["session_id"]: run for run in await crud.get_run_history(db, job_id)}
    assert runs[bad_id]["status"] == "running"
    assert runs[good_id]["status"] == "passed"


@pytest.mark.asyncio
async def test_reaper_expiry_reaches_user_job_end_hook(db):
    from genesis.cc.session_manager import SessionManager

    job_id = await _create_job(db)
    session_id = "reaper-user-job"
    await _create_run(db, job_id, session_id)
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await _create_session(db, session_id, last_activity_at=stale)
    manager = SessionManager(db=db)
    scheduler = UserJobScheduler(db=db)
    manager.add_on_end(scheduler.on_session_end)

    assert await manager.cleanup_stale(max_idle_minutes=60) == 1
    run = (await crud.get_run_history(db, job_id))[0]
    assert run["status"] == "failed"
    assert run["error_message"] == "expired_idle"


@pytest.mark.asyncio
async def test_start_reconciles_before_registering_jobs(db, monkeypatch):
    job_id = await _create_job(db)
    await _create_run(db, job_id)
    scheduler = UserJobScheduler(db=db)
    events = []

    async def reconcile():
        events.append("reconcile")
        return 1

    def register(_job):
        events.append("register")
        return True

    monkeypatch.setattr(scheduler, "reconcile_open_runs", reconcile)
    monkeypatch.setattr(scheduler, "_register_job", register)

    await scheduler.start()
    try:
        assert events == ["reconcile", "register"]
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "session_status", "outcome", "expected_status", "expected_error"),
    [
        ("terminal-failed", "failed", "failed", "failed", "session failed"),
        ("active", "active", None, "running", None),
        (
            "no-session",
            None,
            None,
            "failed",
            "dispatch interrupted before a session was attached",
        ),
        ("missing-row", "missing", None, "failed", "session record not found"),
    ],
)
async def test_start_stop_reconciles_required_boot_cases(
    db, case, session_status, outcome, expected_status, expected_error,
):
    job_id = await _create_job(db)
    session_id = None if case == "no-session" else f"boot-{case}"
    run_id = await crud.record_run_start(db, job_id=job_id)
    if session_id is not None:
        assert await crud.attach_run_session(db, run_id, session_id)
    if session_status not in (None, "missing"):
        await _create_session(db, session_id, status=session_status, outcome=outcome)
    scheduler = UserJobScheduler(db=db)

    await scheduler.start()
    try:
        run = (await crud.get_run_history(db, job_id))[0]
        job = await crud.get_job(db, job_id)
        assert scheduler.is_running
        assert run["status"] == expected_status
        assert job["last_status"] == expected_status
        assert run["error_message"] == expected_error
        assert job["failure_count"] == (1 if expected_status == "failed" else 0)
    finally:
        await scheduler.stop()

    assert not scheduler.is_running


@pytest.mark.asyncio
async def test_reconcile_failure_does_not_block_scheduler_start(db, monkeypatch):
    scheduler = UserJobScheduler(db=db)

    async def broken_reconcile():
        raise RuntimeError("reconcile failed")

    monkeypatch.setattr(scheduler, "reconcile_open_runs", broken_reconcile)
    await scheduler.start()
    try:
        assert scheduler.is_running
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_empty_state_start_and_reconcile(db):
    scheduler = UserJobScheduler(db=db)

    assert await scheduler.reconcile_open_runs() == 0
    await scheduler.on_session_end("no-session")
    await scheduler.start()
    assert scheduler.is_running
    await scheduler.stop()


@pytest.mark.asyncio
async def test_init_registers_hook_before_scheduler_start(db, monkeypatch):
    from genesis.runtime.init import user_jobs as user_jobs_init

    events = []

    class SessionManager:
        def add_on_end(self, hook):
            events.append(("hook", hook))

    original_start = UserJobScheduler.start

    async def start(self):
        assert events and events[0][0] == "hook"
        events.append(("start", self))
        await original_start(self)

    monkeypatch.setattr(UserJobScheduler, "start", start)
    runtime = SimpleNamespace(
        _db=db,
        _event_bus=None,
        _session_manager=SessionManager(),
        _user_job_scheduler=None,
    )

    await user_jobs_init.init(runtime)
    try:
        assert events[0][1] == runtime._user_job_scheduler.on_session_end
        assert events[1] == ("start", runtime._user_job_scheduler)
    finally:
        await runtime._user_job_scheduler.stop()


@pytest.mark.asyncio
async def test_init_without_session_manager_still_starts(db, monkeypatch):
    from genesis.runtime.init import user_jobs as user_jobs_init

    started = []

    async def start(self):
        started.append(self)

    monkeypatch.setattr(UserJobScheduler, "start", start)
    runtime = SimpleNamespace(
        _db=db,
        _event_bus=None,
        _user_job_scheduler=None,
    )

    await user_jobs_init.init(runtime)

    assert started == [runtime._user_job_scheduler]
