"""Native managed receipts: persisted ownership before hooks and effects."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
from genesis.cc.managed_session import _HOLD_PARK_S, prepared_metadata, reconcile_orphans
from genesis.cc.session_manager import SessionManager
from genesis.cc.types import CCInvocation, CCOutput
from genesis.db.crud import cc_sessions


def output(**kwargs):
    defaults = dict(
        session_id="cc",
        text="answer",
        model_used="sonnet",
        cost_usd=0.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        exit_code=0,
    )
    defaults.update(kwargs)
    return CCOutput(**defaults)


@pytest.fixture
def native(db, tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    invoker = SimpleNamespace(run_streaming=AsyncMock(return_value=output()))
    manager = SessionManager(db=db)
    runtime = SimpleNamespace(
        _db=db,
        _autonomy_manager=SimpleNamespace(
            effective_level=AsyncMock(return_value=3),
            get_state=AsyncMock(return_value=None),
        ),
    )
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=manager,
        runtime=runtime,
        config_builder=AsyncMock(),
    )
    runner._build_invocation = lambda req, sid: CCInvocation(prompt=req.prompt)
    return runner, manager, invoker


def request(**kwargs):
    return DirectSessionRequest(
        prompt="a synthetic conversation",
        source_tag="agent_api",
        caller_context="agent_api:synthetic",
        notify=False,
        managed=True,
        **kwargs,
    )


async def prepare(db, req, sid=None, **changes):
    sid = sid or str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    meta = prepared_metadata(req)
    meta.update(changes)
    await cc_sessions.create(
        db,
        id=sid,
        session_type="background_task",
        model=str(req.model),
        effort=str(req.effort),
        source_tag=req.source_tag,
        channel="agent",
        origin_class="external_untrusted",
        metadata=json.dumps(meta),
        started_at=now,
        last_activity_at=now,
    )
    return sid


async def settled(runner, sid):
    task = runner._active[sid]
    await asyncio.wait_for(asyncio.shield(task), 5)
    await asyncio.sleep(0)


async def reached(event):
    await asyncio.wait_for(event.wait(), 5)


async def test_adopt_registers_before_start_hook_and_keeps_receipt(db, native):
    runner, manager, invoker = native
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(sid, kind, source):
        assert runner.is_active(sid)
        assert source == "agent_api"
        entered.set()
        await release.wait()

    manager.add_on_start(hook)
    req = request()
    sid = await prepare(db, req)
    assert await runner.adopt(sid, req) == sid
    await reached(entered)
    invoker.run_streaming.assert_not_awaited()
    with pytest.raises(ValueError):
        await runner.adopt(sid, req)
    release.set()
    await settled(runner, sid)
    row = await cc_sessions.get_by_id(db, sid)
    meta = json.loads(row["metadata"])
    assert row["status"] == "completed"
    assert meta["native_state"] == "succeeded"
    assert meta["result_committed"] is True
    assert invoker.run_streaming.await_count == 1


async def test_cancel_before_first_step_has_no_effects(db, native):
    runner, manager, invoker = native
    hook = AsyncMock()
    manager.add_on_start(hook)
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    assert runner.cancel(sid)
    await settled(runner, sid)
    invoker.run_streaming.assert_not_awaited()
    hook.assert_not_awaited()
    assert (
        json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])["native_state"]
        == "cancelled"
    )
    assert not runner.cancel(sid)


async def test_cancel_holds_capacity_until_cleanup_confirmed(db, native):
    runner, _, invoker = native
    entered, draining, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await drained.wait()

    invoker.run_streaming.side_effect = execute
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await reached(entered)
    assert runner.cancel(sid)
    await reached(draining)
    assert runner.is_active(sid)
    assert (await cc_sessions.get_by_id(db, sid))["status"] == "active"
    runner.cancel(sid)  # repeated cancellation cannot abandon cleanup
    await asyncio.sleep(0)
    assert runner.is_active(sid)
    drained.set()
    await settled(runner, sid)
    assert not runner.is_active(sid)


async def test_expired_receipt_does_not_start(db, native):
    runner, _, invoker = native
    req = request()
    sid = await prepare(db, req, deadline_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    await runner.adopt(sid, req)
    await settled(runner, sid)
    invoker.run_streaming.assert_not_awaited()
    assert (
        json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])["error"]
        == "deadline_exceeded"
    )


async def test_model_error_is_failed_and_full_output_is_private(db, native):
    from pathlib import Path

    runner, _, invoker = native
    text = "é" * 25000
    invoker.run_streaming.return_value = output(
        text=text,
        is_error=True,
        error_message="private detail",
        cost_usd=0.1,
    )
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    row = await cc_sessions.get_by_id(db, sid)
    meta = json.loads(row["metadata"])
    assert row["status"] == "failed"
    assert row["cost_usd"] == 0.1
    assert meta["native_state"] == "failed"
    assert meta["error"] == "execution_failed"
    assert len(meta["output_text"]) == 20000
    path = Path(meta["result_path"])
    assert path.read_text(encoding="utf-8") == text
    assert path.stat().st_mode & 0o777 == 0o600


async def test_live_receipt_survives_stale_reaper_then_orphan_fails(db, native):
    runner, manager, invoker = native
    entered = asyncio.Event()

    async def execute(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    invoker.run_streaming.side_effect = execute
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await reached(entered)
    await cc_sessions.update_activity(db, sid, last_activity_at="2000-01-01T00:00:00+00:00")
    assert await manager.cleanup_stale() == 0
    await runner.shutdown()
    # A committed receipt left across a runtime death is never replayed.
    orphan = await prepare(db, req)
    assert await reconcile_orphans(db) == 1
    meta = json.loads((await cc_sessions.get_by_id(db, orphan))["metadata"])
    assert meta["native_state"] == "failed"
    assert meta["error"] == "outcome_unknown"
    assert not await cc_sessions.delete(db, orphan)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"profile": "full"},
        {"tool_exceptions": ("Bash",)},
        {"timeout_s": 7201},
        {"timeout_s": True},
        {"timeout_s": 0},
    ],
)
def test_managed_request_rejects_authority_or_bound_overrides(kwargs):
    with pytest.raises(ValueError):
        request(**kwargs)


async def test_adoption_construction_failure_settles_only_valid_receipt(db, native, monkeypatch):
    runner, _, invoker = native
    req = request()
    sid = await prepare(db, req)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic scheduling failure")

    monkeypatch.setattr("genesis.cc.direct_session.tracked_task", fail)
    with pytest.raises(RuntimeError):
        await runner.adopt(sid, req)
    row = await cc_sessions.get_by_id(db, sid)
    assert row["status"] == "failed"
    assert json.loads(row["metadata"])["error"] == "not_started"
    assert not runner.is_active(sid)
    invoker.run_streaming.assert_not_awaited()
    with pytest.raises(ValueError):
        await runner.adopt(sid, req)


@pytest.mark.parametrize(
    "alteration",
    [
        {"managed_fingerprint": "mismatch"},
        {"native_state": "running"},
    ],
)
async def test_rejected_adoption_does_not_change_receipt(db, native, alteration):
    runner, _, invoker = native
    req = request()
    sid = await prepare(db, req, **alteration)
    before = await cc_sessions.get_by_id(db, sid)
    with pytest.raises(ValueError):
        await runner.adopt(sid, req)
    assert await cc_sessions.get_by_id(db, sid) == before
    invoker.run_streaming.assert_not_awaited()


@pytest.mark.parametrize(
    "returned",
    [
        output(result_received=False, text="partial", exit_code=7),
        output(bg_truncated=True, text="partial"),
    ],
)
async def test_incomplete_output_never_claims_success(db, native, returned):
    runner, _, invoker = native
    invoker.run_streaming.return_value = returned
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    row = await cc_sessions.get_by_id(db, sid)
    assert row["status"] == "failed"
    assert json.loads(row["metadata"])["result_partial"] is True


async def test_deadline_includes_start_hook_wait(db, native):
    runner, manager, invoker = native
    entered = asyncio.Event()

    async def hook(*args):
        entered.set()
        await asyncio.Event().wait()

    manager.add_on_start(hook)
    req = request(timeout_s=1)
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await reached(entered)
    await settled(runner, sid)
    invoker.run_streaming.assert_not_awaited()
    assert (
        json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])["error"]
        == "deadline_exceeded"
    )


async def test_managed_checks_effective_level_and_existing_circuit_breaker(db, native):
    runner, _, invoker = native
    mgr = runner._rt._autonomy_manager
    mgr.get_state.return_value = SimpleNamespace(
        total_successes=0,
        total_corrections=20,
        current_level=3,
    )
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    invoker.run_streaming.assert_not_awaited()
    assert (await cc_sessions.get_by_id(db, sid))["status"] == "failed"
    mgr.get_state.return_value = None
    mgr.effective_level.return_value = 1
    req = request(profile="interact")
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    invoker.run_streaming.assert_not_awaited()


async def test_spawn_cannot_bypass_prepared_mode(native):
    runner, _, invoker = native
    with pytest.raises(ValueError):
        await runner.spawn(request())
    invoker.run_streaming.assert_not_awaited()


async def test_private_diagnostics_are_task_local_and_child_traces_are_disabled(
    tmp_path, caplog, monkeypatch
):
    import logging

    from genesis.cc.invoker import CCInvoker
    from genesis.util.execution_privacy import private_execution

    monkeypatch.setattr(
        "genesis.observability.spans.current_trace_context", lambda: ("trace", "span")
    )
    invoker = CCInvoker(claude_path="/usr/bin/claude")
    logger = logging.getLogger("genesis.cc.invoker")
    caplog.set_level(logging.WARNING, logger=logger.name)

    async def ordinary():
        logger.warning("ordinary diagnostic")
        return invoker._build_env(CCInvocation(prompt="ordinary"))

    task = asyncio.create_task(ordinary())
    with private_execution():
        logger.warning("synthetic private diagnostic")
        env = invoker._build_env(CCInvocation(prompt="private"))
        assert "GENESIS_TRACE_ID" not in env
        assert "GENESIS_PARENT_SPAN_ID" not in env
        ordinary_env = await task
    assert ordinary_env["GENESIS_TRACE_ID"] == "trace"
    markers = {"ordinary diagnostic", "synthetic private diagnostic"}
    assert [
        r.getMessage()
        for r in caplog.records
        if r.name == logger.name and r.getMessage() in markers
    ] == ["ordinary diagnostic"]


async def test_strong_drain_refusal_and_repeated_cancel_retain_ownership(monkeypatch):
    from unittest.mock import Mock

    from genesis.util.proc_kill import confirm_process_exit

    gone = False
    proc = SimpleNamespace(pid=99999, returncode=None, kill=Mock(side_effect=PermissionError))

    def probe(pid, sig):
        if gone:
            raise ProcessLookupError
        raise PermissionError

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", probe)
    task = asyncio.create_task(confirm_process_exit(proc))
    await asyncio.sleep(0.11)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.11)
    assert not task.done()
    gone = True
    proc.returncode = -9
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


async def test_cancel_during_creation_owns_handle_and_drains_before_return(monkeypatch):
    from genesis.util.proc_kill import spawn_owned

    entered, release = asyncio.Event(), asyncio.Event()
    launches = []
    proc = SimpleNamespace(pid=99999, returncode=-9)

    async def create(*args, **kwargs):
        launches.append(asyncio.current_task())
        entered.set()
        await release.wait()
        return proc

    confirm = AsyncMock()
    monkeypatch.setattr("genesis.util.proc_kill.asyncio.create_subprocess_exec", create)
    monkeypatch.setattr("genesis.util.proc_kill.confirm_process_exit", confirm)
    task = asyncio.create_task(spawn_owned("synthetic-cli"))
    await reached(entered)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert launches[0].cancelling() == 0
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    confirm.assert_awaited_once_with(proc)


@pytest.mark.parametrize("failure", [OSError, FileNotFoundError])
async def test_creation_without_handle_is_explicitly_uncertain(monkeypatch, failure):
    from genesis.util.proc_kill import ProcessCleanupUncertain, spawn_owned

    create = AsyncMock(side_effect=failure("synthetic transport setup failure"))
    monkeypatch.setattr("genesis.util.proc_kill.asyncio.create_subprocess_exec", create)
    with pytest.raises(ProcessCleanupUncertain, match="creation is unconfirmed"):
        await spawn_owned("synthetic-cli")
    create.assert_awaited_once()


async def test_uncertain_creation_keeps_registered_receipt_through_cancellation(
    db, native, monkeypatch
):
    from genesis.util.proc_kill import ProcessCleanupUncertain

    runner, _, invoker = native
    invoker.run_streaming.side_effect = ProcessCleanupUncertain("synthetic uncertainty")
    paused, teardown = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep

    class TestTeardown(BaseException):
        """Only the test harness can end the deliberately unbounded hold."""

    async def pause(seconds):
        if seconds != _HOLD_PARK_S:
            return await original_sleep(seconds)
        paused.set()
        await teardown.wait()
        raise TestTeardown

    monkeypatch.setattr("genesis.cc.managed_session.asyncio.sleep", pause)
    sid = await prepare(db, request())
    await runner.adopt(sid, request())
    task = runner._active[sid]
    await reached(paused)
    try:
        for _ in range(2):
            task.cancel()
            await original_sleep(0)
        assert runner.is_active(sid)
        assert not task.done()
        row = await cc_sessions.get_by_id(db, sid)
        assert row["status"] == "active"
        assert json.loads(row["metadata"])["cleanup_unconfirmed"] is True
        invoker.run_streaming.assert_awaited_once()
    finally:
        # Harness-only teardown; this is not a production recovery API or proof
        # of a confirmed terminal outcome after an ambiguous startup failure.
        teardown.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_prepared_insert_can_rollback_without_committing(db):
    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        db,
        id="uncommitted",
        session_type="background_task",
        model="sonnet",
        started_at=now,
        last_activity_at=now,
        commit=False,
    )
    await db.rollback()
    assert await cc_sessions.get_by_id(db, "uncommitted") is None


async def test_publication_failure_is_explicit_and_never_claims_full_result(
    db, native, monkeypatch
):
    runner, _, invoker = native

    def fail(*args, **kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr("genesis.cc.managed_session.atomic_write_text", fail)
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    row = await cc_sessions.get_by_id(db, sid)
    meta = json.loads(row["metadata"])
    assert row["status"] == "failed"
    assert meta["error"] == "result_unavailable"
    assert meta["result_committed"] is False
    assert meta["result_path"] is None
    assert invoker.run_streaming.await_count == 1


async def test_persistence_failure_retains_capacity_and_retry_does_not_double_cost(
    db, native, monkeypatch
):
    import sqlite3

    runner, manager, invoker = native
    invoker.run_streaming.return_value = output(cost_usd=0.2)
    original_complete = manager.complete
    failed, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def flaky_complete(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            failed.set()
            raise sqlite3.OperationalError("synthetic temporary database failure")
        await release.wait()
        await original_complete(*args, **kwargs)

    monkeypatch.setattr(manager, "complete", flaky_complete)
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await reached(failed)
    assert runner.is_active(sid)
    assert (await cc_sessions.get_by_id(db, sid))["status"] == "active"
    release.set()
    await settled(runner, sid)
    row = await cc_sessions.get_by_id(db, sid)
    assert row["status"] == "completed"
    assert row["cost_usd"] == 0.2
    assert attempts == 2
    assert invoker.run_streaming.await_count == 1


async def test_real_invoker_exception_contract_is_classified_without_raw_error(db, native):
    from genesis.cc.exceptions import CCProcessError

    runner, _, invoker = native
    invoker.run_streaming.side_effect = CCProcessError("synthetic provider diagnostic")
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    meta = json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])
    assert meta["native_state"] == "failed"
    assert meta["error"] == "execution_failed"
    assert "synthetic provider diagnostic" not in json.dumps(meta)


async def test_stdin_failure_still_drains_when_both_kill_methods_refuse(
    db, native, monkeypatch, caplog
):
    from genesis.cc.invoker import CCInvoker

    runner, _, _ = native
    invoker = CCInvoker(claude_path="synthetic-cli")
    probing, gone = asyncio.Event(), asyncio.Event()
    proc = SimpleNamespace(pid=99999, returncode=None)

    def refuse(*args):
        raise PermissionError("synthetic private kill refusal")

    def probe(pid, sig):
        if sig == 0:
            probing.set()
            if gone.is_set():
                proc.returncode = -9
                raise ProcessLookupError
        refuse()

    def broken_write(data):
        raise BrokenPipeError("synthetic private stdin failure")

    proc.kill = refuse
    proc.stdin = SimpleNamespace(write=broken_write)
    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", probe)
    monkeypatch.setattr("genesis.cc.invoker.set_oom_score_adj", lambda *args: None)
    monkeypatch.setattr("genesis.cc.invoker._get_scope_args", AsyncMock(return_value=[]))
    monkeypatch.setattr("genesis.cc.invoker.spawn_owned", AsyncMock(return_value=proc))
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    monkeypatch.setattr(invoker, "_cc_version", AsyncMock(return_value=None))
    monkeypatch.setattr(
        invoker,
        "_build_args",
        lambda *args, **kwargs: ["synthetic-cli", "-p", "--output-format", "json"],
    )
    runner._invoker = invoker
    sid = await prepare(db, request())
    await runner.adopt(sid, request())
    await asyncio.wait_for(probing.wait(), 2)
    try:
        assert runner.is_active(sid)
        assert proc.returncode is None
        assert (await cc_sessions.get_by_id(db, sid))["status"] == "active"
        assert "synthetic private kill refusal" not in caplog.text
        assert "synthetic private stdin failure" not in caplog.text
    finally:
        gone.set()
        await settled(runner, sid)
    meta = json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])
    assert meta["native_state"] == "failed"
    assert meta["error"] == "execution_failed"


@pytest.mark.parametrize("behavior", ["result", "eof", "cancel"])
@pytest.mark.parametrize("scoped", [False, True], ids=["process-group", "user-scope"])
async def test_native_subprocess_lifecycle_and_provenance(
    db, native, tmp_path, monkeypatch, behavior, caplog, scoped
):
    """Real OS process, native invoker and SQLite; scripted CLI, no provider spend."""
    import logging
    import os
    import sys

    from genesis.cc.invoker import CCInvoker, _get_scope_args
    from genesis.util.proc_kill import spawn_owned

    script = tmp_path / "synthetic_cli.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "sys.stdin.read()\n"
        "print('synthetic private stderr', file=sys.stderr, flush=True)\n"
        + (
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'session_id':'synthetic-cc','result':json.dumps({'origin':os.environ.get('GENESIS_SESSION_ORIGIN'),"
            "'supervised':os.environ.get('GENESIS_SESSION_SUPERVISED'),"
            "'traced':bool(os.environ.get('GENESIS_TRACE_ID'))})}), flush=True)\n"
            "time.sleep(30)\n"
            if behavior == "result"
            else "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text',"
            "'text':'synthetic partial response'}]}}), flush=True)\n"
            + ("sys.exit(7)\n" if behavior == "eof" else "time.sleep(30)\n")
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)
    runner, _, _ = native
    invoker = CCInvoker(claude_path=sys.executable)
    monkeypatch.setattr(
        invoker,
        "_build_args",
        lambda *args, **kwargs: [str(script), "-p", "--output-format", "json"],
    )
    prefix = await _get_scope_args() if scoped else []
    if scoped and not prefix:
        pytest.skip("Native user scope is unavailable on this test host")
    monkeypatch.setattr("genesis.cc.invoker._get_scope_args", AsyncMock(return_value=prefix))
    launch = AsyncMock(wraps=spawn_owned)
    monkeypatch.setattr("genesis.cc.invoker.spawn_owned", launch)
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    monkeypatch.setattr(invoker, "_cc_version", AsyncMock(return_value=None))
    runner._invoker = invoker
    spawned = asyncio.Event()
    pids = []

    async def on_spawn(pid):
        pids.append(pid)
        spawned.set()

    runner._build_invocation = lambda req, sid: CCInvocation(
        prompt=req.prompt,
        working_dir=str(tmp_path),
        on_spawn=on_spawn,
    )
    caplog.set_level(logging.INFO)
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    if behavior == "cancel":
        await asyncio.wait_for(spawned.wait(), 2)
        assert runner.cancel(sid)
    await settled(runner, sid)
    meta = json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])
    expected = {"result": "succeeded", "eof": "failed", "cancel": "cancelled"}
    assert meta["native_state"] == expected[behavior]
    if behavior == "result":
        assert json.loads(meta["output_text"]) == {
            "origin": "external_untrusted",
            "supervised": None,
            "traced": False,
        }
    elif behavior == "eof":
        assert meta["result_partial"] is True
    assert len(pids) == 1
    assert launch.await_count == 1
    with pytest.raises(ProcessLookupError):
        os.killpg(pids[0], 0)
    assert "synthetic private stderr" not in caplog.text


@pytest.mark.parametrize(
    "sid",
    # Fixed literals: a random id would rename the test on every collection.
    [
        "../../.genesis",
        "a/b",
        "receipt",
        "5F0C6E1A-3B7D-4C2E-9A1F-8D4B2C6E0A13",
        "{5f0c6e1a-3b7d-4c2e-9a1f-8d4b2c6e0a13}",
    ],
)
async def test_only_canonical_uuid_receipts_are_adoptable(db, native, tmp_path, sid):
    runner, _, invoker = native
    victim = tmp_path / ".genesis" / "keep"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("kept")
    req = request()
    await prepare(db, req, sid=sid)
    before = await cc_sessions.get_by_id(db, sid)
    with pytest.raises(ValueError):
        await runner.adopt(sid, req)
    assert await cc_sessions.get_by_id(db, sid) == before
    assert not runner.is_active(sid)
    invoker.run_streaming.assert_not_awaited()
    assert victim.read_text() == "kept"


@pytest.mark.parametrize("sid", ["", ".", "..", "../x", "a/b", "/abs"])
def test_sandbox_root_refuses_anything_but_one_path_component(sid):
    from genesis.cc.direct_session import _bg_session_root

    with pytest.raises(ValueError):
        _bg_session_root(sid)


async def test_adopted_request_is_a_snapshot_the_caller_cannot_mutate(db, native):
    runner, manager, _ = native
    entered, release = asyncio.Event(), asyncio.Event()
    built = []

    async def hook(sid, kind, source):
        entered.set()
        await release.wait()

    def build(req, sid):
        built.append(req.skills)
        return CCInvocation(prompt=req.prompt)

    manager.add_on_start(hook)
    runner._build_invocation = build
    skills = ["synthetic-skill"]
    req = request(skills=skills)
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await reached(entered)
    skills.append("injected-after-verification")
    release.set()
    await settled(runner, sid)
    assert built == [["synthetic-skill"]]


async def test_uncertain_cleanup_keeps_its_runner_slot(db, native, monkeypatch):
    from genesis.util.proc_kill import ProcessCleanupUncertain

    runner, _, invoker = native
    invoker.run_streaming.side_effect = ProcessCleanupUncertain("synthetic uncertainty")
    paused, teardown = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep

    class TestTeardown(BaseException):
        """Only the test harness can end the deliberately unbounded hold."""

    async def pause(seconds):
        if seconds != _HOLD_PARK_S:
            return await original_sleep(seconds)
        paused.set()
        await teardown.wait()
        raise TestTeardown

    monkeypatch.setattr("genesis.cc.managed_session.asyncio.sleep", pause)
    free = runner._semaphore._value
    sid = await prepare(db, request())
    await runner.adopt(sid, request())
    task = runner._active[sid]
    await reached(paused)
    try:
        task.cancel()
        await original_sleep(0)
        # The possibly live child still counts against the runner's bound.
        assert runner._semaphore._value == free - 1
        assert runner.is_active(sid)
    finally:
        teardown.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_orphan_reconciliation_ignores_unprepared_agent_rows(db):
    now = datetime.now(UTC).isoformat()
    sid = str(uuid.uuid4())
    await cc_sessions.create(
        db,
        id=sid,
        session_type="background_task",
        model="sonnet",
        source_tag="agent_api",
        metadata=json.dumps({"note": "not a prepared receipt"}),
        started_at=now,
        last_activity_at=now,
    )
    assert await reconcile_orphans(db) == 0
    row = await cc_sessions.get_by_id(db, sid)
    assert row["status"] == "active"
    assert "native_state" not in json.loads(row["metadata"])


async def test_managed_telemetry_counts_every_tool_block_on_a_line(db, native):
    from genesis.cc.types import StreamEvent

    runner, _, invoker = native
    raw = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "plan"},
                {"type": "tool_use", "name": "Read", "input": {}},
                {"type": "tool_use", "name": "Grep", "input": {}},
            ]
        },
    }

    async def execute(invocation, on_event=None):
        await on_event(StreamEvent.from_raw(raw))
        return output()

    invoker.run_streaming.side_effect = execute
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    meta = json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])
    assert meta["tools_summary"] == {"Read": 1, "Grep": 1}


@pytest.mark.parametrize(
    ("native_state", "status"),
    [("succeeded", "completed"), ("failed", "failed"), ("cancelled", "failed")],
)
async def test_orphan_with_committed_outcome_is_finished_not_overwritten(
    db, native_state, status
):
    req = request()
    sid = await prepare(
        db,
        req,
        native_state=native_state,
        result_committed=True,
        result_path="/synthetic/result.md",
    )
    assert await reconcile_orphans(db) == 1
    row = await cc_sessions.get_by_id(db, sid)
    meta = json.loads(row["metadata"])
    assert row["status"] == status
    assert meta["native_state"] == native_state
    assert meta["result_committed"] is True
    assert "error" not in meta


async def test_successful_managed_run_lets_the_child_flush_before_the_group_kill(
    db, native, tmp_path, monkeypatch
):
    """After a clean result the child is only asked to stop. Its SIGTERM handler
    must get to run (flush) before the confirming group kill."""
    import sys

    from genesis.cc.invoker import CCInvoker

    flushed = tmp_path / "flushed"
    script = tmp_path / "synthetic_cli.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, signal, sys, time\n"
        "def stop(*_):\n"
        "    time.sleep(0.3)\n"
        f"    open({str(flushed)!r}, 'w').write('ok')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'session_id':'synthetic-cc','result':'done'}), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    runner, _, _ = native
    invoker = CCInvoker(claude_path=sys.executable)
    monkeypatch.setattr(
        invoker, "_build_args", lambda *a, **k: [str(script), "-p", "--output-format", "json"]
    )
    monkeypatch.setattr("genesis.cc.invoker._get_scope_args", AsyncMock(return_value=[]))
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    monkeypatch.setattr(invoker, "_cc_version", AsyncMock(return_value=None))
    runner._invoker = invoker
    runner._build_invocation = lambda req, sid: CCInvocation(
        prompt=req.prompt, working_dir=str(tmp_path)
    )
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    meta = json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])
    assert meta["native_state"] == "succeeded"
    assert flushed.read_text() == "ok"


async def test_held_uncertain_state_is_written_once(db, native, monkeypatch, caplog):
    from genesis.cc import managed_session
    from genesis.util.proc_kill import ProcessCleanupUncertain

    runner, _, invoker = native
    invoker.run_streaming.side_effect = ProcessCleanupUncertain("synthetic uncertainty")
    writes = []
    original_update = managed_session.update_state

    async def counting_update(db_, sid_, state, **fields):
        if fields.get("cleanup_unconfirmed"):
            writes.append(state)
            if len(writes) == 1:
                raise RuntimeError("synthetic transient write failure")
        return await original_update(db_, sid_, state, **fields)

    monkeypatch.setattr(managed_session, "update_state", counting_update)
    parked, teardown = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep
    retry_sleeps = []

    class TestTeardown(BaseException):
        """Only the test harness can end the deliberately unbounded hold."""

    async def pause(seconds):
        if seconds == _HOLD_PARK_S:
            parked.set()
            await teardown.wait()
            raise TestTeardown
        if seconds >= 1:
            retry_sleeps.append(seconds)
            if len(retry_sleeps) > 5:
                raise TestTeardown  # still rewriting: fail instead of spinning
            return await original_sleep(0)
        return await original_sleep(seconds)

    monkeypatch.setattr("genesis.cc.managed_session.asyncio.sleep", pause)
    sid = await prepare(db, request())
    await runner.adopt(sid, request())
    task = runner._active[sid]
    await reached(parked)
    try:
        # One failed write, one successful write, then no further writes.
        assert writes == ["cancelling", "cancelling"]
        assert retry_sleeps == [1.0]
        holds = [
            r for r in caplog.records
            if r.name == "genesis.cc.managed_session" and "cleanup unconfirmed" in r.getMessage()
        ]
        assert len(holds) == 1 and holds[0].levelname == "WARNING"
        row = await cc_sessions.get_by_id(db, sid)
        assert json.loads(row["metadata"])["cleanup_unconfirmed"] is True
    finally:
        teardown.set()
        await asyncio.gather(task, return_exceptions=True)
@pytest.mark.parametrize("failure", ["missing", "not_executable", "bad_cwd"])
async def test_a_popen_launch_failure_is_ordinary_not_uncertain(tmp_path, failure):
    """Popen reaps any child it forked before raising, so nothing is left to own
    and no runner slot should be held for it (#3369)."""
    import sys

    from genesis.util.proc_kill import ProcessCleanupUncertain, spawn_owned

    argv, kwargs = [sys.executable, "-c", "pass"], {}
    if failure == "missing":
        argv = [str(tmp_path / "no-such-cli")]
    elif failure == "not_executable":
        script = tmp_path / "plain.txt"
        script.write_text("not a program")
        argv = [str(script)]
    else:
        kwargs["cwd"] = str(tmp_path / "no-such-dir")
    with pytest.raises(OSError) as raised:
        await spawn_owned(*argv, start_new_session=True, **kwargs)
    assert not isinstance(raised.value, ProcessCleanupUncertain)


async def test_a_failure_after_the_fork_stays_uncertain():
    """An error-pipe read failing after the fork leaves an unreaped child."""
    import os
    import subprocess
    import sys

    from genesis.util.proc_kill import ProcessCleanupUncertain, spawn_owned

    forked, real_read = [], os.read

    def failing_read(fd, n):
        caller = sys._getframe(1)
        if caller.f_code.co_name == "_execute_child" and not forked:
            forked.append(caller.f_locals["self"].pid)
            raise InterruptedError("synthetic: error-pipe read interrupted")
        return real_read(fd, n)

    try:
        with pytest.MonkeyPatch.context() as mp, pytest.raises(ProcessCleanupUncertain):
            mp.setattr(subprocess.os, "read", failing_read)
            await spawn_owned(sys.executable, "-c", "pass", start_new_session=True)
    finally:
        for pid in forked:
            os.waitpid(pid, 0)
    assert forked and forked[0] is not None


async def test_a_stdio_pipe_failure_before_the_fork_is_ordinary():
    """fd exhaustion while Popen makes its stdio pipes creates no child."""
    import errno
    import os
    import subprocess
    import sys

    from genesis.util.proc_kill import ProcessCleanupUncertain, spawn_owned

    real_pipe = os.pipe

    def exhausted_pipe(*args):
        if sys._getframe(1).f_code.co_name == "_get_handles":
            raise OSError(errno.EMFILE, "synthetic: too many open files")
        return real_pipe(*args)

    with pytest.MonkeyPatch.context() as mp, pytest.raises(OSError) as raised:
        mp.setattr(subprocess.os, "pipe", exhausted_pipe)
        await spawn_owned(
            sys.executable, "-c", "pass", stdout=subprocess.PIPE, start_new_session=True
        )
    assert raised.value.errno == errno.EMFILE
    assert not isinstance(raised.value, ProcessCleanupUncertain)


async def test_a_cancel_during_an_ordinary_launch_failure_stays_a_cancel(tmp_path):
    from genesis.util.proc_kill import spawn_owned

    task = asyncio.create_task(spawn_owned(str(tmp_path / "no-such-cli"), start_new_session=True))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unconfirmable_group_becomes_uncertain_after_the_bound(monkeypatch):
    """A group whose absence cannot be observed is reported, not watched forever."""
    import os

    from genesis.util import proc_kill
    from genesis.util.proc_kill import ProcessCleanupUncertain, confirm_process_exit

    def refuse(pgid, sig):
        raise PermissionError("synthetic: liveness unknown")

    monkeypatch.setattr(proc_kill.os, "killpg", refuse)
    proc = SimpleNamespace(pid=os.getpid() + 100000, returncode=None, kill=lambda: None)
    with pytest.raises(ProcessCleanupUncertain):
        await asyncio.wait_for(confirm_process_exit(proc, confirm_timeout_s=0.3), 5)


async def test_slot_is_held_until_finalization_commits(db, native, monkeypatch):
    import sqlite3

    runner, manager, invoker = native
    free = runner._semaphore._value
    original_complete = manager.complete
    failed, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def flaky_complete(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            failed.set()
            raise sqlite3.OperationalError("synthetic temporary database failure")
        await release.wait()
        await original_complete(*args, **kwargs)

    monkeypatch.setattr(manager, "complete", flaky_complete)
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await reached(failed)
    # The subprocess has exited, but the receipt is not persisted yet.
    assert invoker.run_streaming.await_count == 1
    assert runner._semaphore._value == free - 1
    release.set()
    await settled(runner, sid)
    assert runner._semaphore._value == free


async def test_committed_orphan_recovers_its_usage_totals(db):
    req = request()
    sid = await prepare(
        db,
        req,
        native_state="succeeded",
        result_committed=True,
        usage={"cost_usd": 0.25, "input_tokens": 7, "output_tokens": 11},
    )
    assert await reconcile_orphans(db) == 1
    row = await cc_sessions.get_by_id(db, sid)
    assert row["status"] == "completed"
    assert (row["cost_usd"], row["input_tokens"], row["output_tokens"]) == (0.25, 7, 11)


async def test_cancel_keeps_a_recorded_cleanup_uncertainty(db, native, monkeypatch):
    from genesis.util.proc_kill import ProcessCleanupUncertain

    runner, _, invoker = native
    invoker.run_streaming.side_effect = ProcessCleanupUncertain("synthetic uncertainty")
    parked, teardown = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep

    class TestTeardown(BaseException):
        """Only the test harness can end the deliberately unbounded hold."""

    async def pause(seconds):
        if seconds == _HOLD_PARK_S:
            parked.set()
            await teardown.wait()
            raise TestTeardown
        return await original_sleep(seconds)

    monkeypatch.setattr("genesis.cc.managed_session.asyncio.sleep", pause)
    sid = await prepare(db, request())
    await runner.adopt(sid, request())
    task = runner._active[sid]
    await reached(parked)
    try:
        assert runner.cancel(sid)
        for _ in range(20):
            await original_sleep(0.01)
        meta = json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])
        assert meta["error"] == "cleanup_unconfirmed"
        assert meta["cleanup_unconfirmed"] is True
    finally:
        teardown.set()
        await asyncio.gather(task, return_exceptions=True)
async def test_a_launch_failure_frees_the_slot_and_fails_the_receipt(db, native):
    """Runner level: an ordinary launch error fails the receipt and keeps no slot."""
    runner, _, invoker = native
    invoker.run_streaming.side_effect = FileNotFoundError(2, "synthetic", "no-such-cli")
    free = runner._semaphore._value
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await settled(runner, sid)
    row = await cc_sessions.get_by_id(db, sid)
    meta = json.loads(row["metadata"])
    assert row["status"] == "failed"
    assert meta["native_state"] == "failed"
    assert not meta.get("cleanup_unconfirmed")
    assert runner._semaphore._value == free


async def test_usage_is_written_with_the_terminal_state(db, native, monkeypatch):
    """The usage totals ride in the terminal metadata, before set_turn_totals."""
    runner, _, invoker = native
    invoker.run_streaming.return_value = output(cost_usd=0.3, input_tokens=5, output_tokens=9)
    failed, release = asyncio.Event(), asyncio.Event()
    original = cc_sessions.set_turn_totals
    calls = 0

    async def flaky_totals(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            failed.set()
            await release.wait()
            raise RuntimeError("synthetic: died before totals")
        return await original(*args, **kwargs)

    monkeypatch.setattr(cc_sessions, "set_turn_totals", flaky_totals)
    req = request()
    sid = await prepare(db, req)
    await runner.adopt(sid, req)
    await reached(failed)
    meta = json.loads((await cc_sessions.get_by_id(db, sid))["metadata"])
    assert meta["native_state"] == "succeeded"
    assert meta["usage"] == {"cost_usd": 0.3, "input_tokens": 5, "output_tokens": 9}
    release.set()
    await settled(runner, sid)
