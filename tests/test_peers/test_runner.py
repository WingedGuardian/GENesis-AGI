"""Runner ownership boundaries through real cc_sessions persistence."""

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
from genesis.cc.peer_segment import PeerSegment
from genesis.cc.session_manager import SessionManager
from genesis.cc.types import CCOutput, StreamEvent
from genesis.db.crud import cc_sessions
from genesis.peers.session import PeerSessionBinding


@pytest.fixture
async def setup(db, tmp_path, monkeypatch):
    facade = tmp_path / "facade.json"
    facade.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "genesis_peer": {
                        "command": sys.executable,
                        "args": [
                            "-m",
                            "genesis.peers.facade",
                            "--lease-file",
                            str(tmp_path / "lease"),
                        ],
                    }
                }
            }
        )
    )
    facade.chmod(0o600)
    binding = PeerSessionBinding(
        "a" * 32,
        PeerSegment("b" * 32, time.time() + 60, ("mcp__genesis_peer__task_context",)),
        4,
        str(facade),
        str(tmp_path),
    )
    drains = []

    async def drain(_self):
        drains.append("scope")

    monkeypatch.setattr(PeerSegment, "stop_and_drain", drain)
    lifecycle = SimpleNamespace(
        authorize=AsyncMock(),
        begin=AsyncMock(),
        completed=AsyncMock(),
        park=AsyncMock(return_value=False),
        drain=AsyncMock(),
        finish=AsyncMock(),
    )
    for name in ("authorize", "begin", "completed", "drain", "finish"):
        getattr(lifecycle, name).return_value = None
    output = CCOutput("cli-id", "result" * 5000, "sonnet", 0, 0, 0, 1, 0)
    invoker = SimpleNamespace(run_streaming=AsyncMock(return_value=output))
    manager = SessionManager(db=db, invoker=invoker, day_boundary_hour=0)
    runtime = SimpleNamespace(
        _db=db,
        _peer_session_lifecycle=lifecycle,
        _autonomy_manager=SimpleNamespace(
            get_state=AsyncMock(
                return_value=SimpleNamespace(
                    current_level=2, total_corrections=0, total_successes=0
                )
            )
        ),
    )
    builder = SimpleNamespace(_load_identity_block=lambda: "SOUL and VOICE")
    runner = DirectSessionRunner(
        invoker=invoker, session_manager=manager, config_builder=builder, runtime=runtime
    )
    request = DirectSessionRequest(
        prompt="peer request", source_tag="peer_api", notify=False, peer_binding=binding
    )
    yield SimpleNamespace(
        runner=runner,
        runtime=runtime,
        invoker=invoker,
        manager=manager,
        lifecycle=lifecycle,
        request=request,
        binding=binding,
        output=output,
        drains=drains,
    )
    await runner.shutdown(grace_s=2)


async def test_result_is_private_atomic_full_before_success(db, setup):
    s = setup

    async def finish(binding, session_id, result):
        row = await cc_sessions.get_by_id(db, session_id)
        metadata = json.loads(row["metadata"])
        assert row["status"] == "completed"
        assert metadata["autonomy_ceiling"] == 2
        assert metadata["peer_segment_id"] == binding.segment.segment_id
        assert metadata["transcript_path"] == ""
        assert len(metadata["output_text"]) == 20000
        path = Path(result["artifact_path"])
        assert path.read_text() == s.output.text
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700

    s.lifecycle.finish.side_effect = finish
    session_id = await s.runner.spawn(s.request)
    task = s.runner._active[session_id]
    result = await task
    assert result.success
    invocation = s.invoker.run_streaming.call_args.args[0]
    assert invocation.system_prompt == "SOUL and VOICE"
    assert invocation.peer_segment is s.binding.segment
    assert invocation.origin == "external_untrusted"
    assert s.drains == ["scope"]
    assert s.runner._semaphore._value == 2


async def test_cancel_before_first_run_still_drains_and_records(db, setup):
    s = setup
    session_id = await s.runner.spawn(s.request)
    task = s.runner._active[session_id]
    assert s.runner.cancel(session_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await cc_sessions.get_by_id(db, session_id))["status"] == "failed"
    s.invoker.run_streaming.assert_not_called()
    s.lifecycle.drain.assert_awaited_once()
    assert s.drains == ["scope"]
    assert s.lifecycle.finish.call_args.args[2]["cancelled"]
    assert s.runner._semaphore._value == 2
    assert not s.runner.cancel(session_id)
    assert not s.runner.cancel("missing")


async def test_timely_result_cleanup_does_not_consume_execution_budget(setup):
    s = setup
    binding = replace(s.binding, segment=replace(s.binding.segment, deadline_at=time.time() + 0.2))
    request = replace(s.request, peer_binding=binding)

    async def invoke(invocation, on_event):
        await on_event(StreamEvent("result"))
        await asyncio.sleep(0.3)
        return s.output

    s.invoker.run_streaming.side_effect = invoke
    session_id = await s.runner.spawn(request)
    result = await s.runner._active[session_id]
    assert result.success
    assert result.duration_s < 0.2
    s.lifecycle.completed.assert_awaited_once()
    observed_binding, observed_at, elapsed = s.lifecycle.completed.call_args.args
    assert observed_binding == binding
    assert observed_at < binding.segment.deadline_at
    assert elapsed < 0.2
    assert s.lifecycle.finish.call_args.args[2]["cleanup_confirmed"]


async def test_completion_proof_refusal_withholds_fallback_output(setup):
    s = setup
    s.lifecycle.completed.side_effect = RuntimeError("Completion refused")
    session_id = await s.runner.spawn(s.request)
    result = await s.runner._active[session_id]
    assert not result.success
    assert result.output_text == ""
    assert s.lifecycle.finish.call_args.args[2]["artifact_path"] is None


async def test_finite_completion_tail_stops_hanging_post_result_callback(setup, monkeypatch):
    s = setup
    monkeypatch.setattr("genesis.peers.runner._PEER_COMPLETION_TAIL_S", 0.05)

    async def invoke(invocation, on_event):
        await on_event(StreamEvent("result"))
        await asyncio.Event().wait()

    s.invoker.run_streaming.side_effect = invoke
    session_id = await s.runner.spawn(s.request)
    result = await s.runner._active[session_id]
    assert not result.success
    s.lifecycle.completed.assert_awaited_once()
    assert not s.lifecycle.finish.call_args.args[2]["expired"]
    assert s.lifecycle.finish.call_args.args[2]["cleanup_confirmed"]
    assert s.runner._semaphore._value == 2


async def test_cancel_waiting_for_slot_does_not_release_unowned_capacity(db, setup):
    s = setup
    await s.runner._semaphore.acquire()
    await s.runner._semaphore.acquire()
    session_id = await s.runner.spawn(s.request)
    task = s.runner._active[session_id]
    await asyncio.sleep(0)
    assert s.runner.cancel(session_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.runner._semaphore._value == 0
    s.invoker.run_streaming.assert_not_called()
    assert (await cc_sessions.get_by_id(db, session_id))["status"] == "failed"
    s.runner._semaphore.release()
    s.runner._semaphore.release()


async def test_unknown_broker_cleanup_retains_capacity_and_disables_peers(setup):
    s = setup
    s.lifecycle.drain.side_effect = RuntimeError("private error")
    session_id = await s.runner.spawn(s.request)
    result = await s.runner._active[session_id]
    assert not result.success
    assert s.runner._semaphore._value == 1
    assert s.runner._peer_cleanup_holds[session_id].acquired
    with pytest.raises(RuntimeError, match="reconciliation"):
        await s.runner.spawn(s.request)
    assert not s.lifecycle.finish.call_args.args[2]["cleanup_confirmed"]


@pytest.mark.parametrize(
    "unavailable", ["missing_lifecycle", "missing_autonomy", "missing_state", "denied"]
)
async def test_no_readiness_fallback_or_session_creation(db, setup, unavailable):
    s = setup
    if unavailable == "missing_lifecycle":
        s.runtime._peer_session_lifecycle = None
    elif unavailable == "missing_autonomy":
        s.runtime._autonomy_manager = None
    elif unavailable == "missing_state":
        s.runtime._autonomy_manager.get_state.return_value = None
    else:
        s.lifecycle.authorize.side_effect = PermissionError("Denied")
    with pytest.raises((RuntimeError, PermissionError)):
        await s.runner.spawn(s.request)
    assert not s.runner._active
    cursor = await db.execute("SELECT count(*) FROM cc_sessions")
    assert (await cursor.fetchone())[0] == 0


async def test_empty_success_still_has_full_artifact(setup):
    s = setup
    s.output = replace(s.output, text="")
    s.invoker.run_streaming.return_value = s.output
    session_id = await s.runner.spawn(s.request)
    assert (await s.runner._active[session_id]).success
    assert Path(s.lifecycle.finish.call_args.args[2]["artifact_path"]).read_text() == ""


async def test_tool_telemetry_has_names_only_and_no_unlisted_tools(setup):
    s = setup

    async def invoke(invocation, on_event):
        await on_event(
            StreamEvent(
                "tool_use",
                tool_name=s.binding.segment.tools[0],
                tool_input={"private": "never persisted"},
            )
        )
        await on_event(StreamEvent("tool_use", tool_name="Bash", tool_input={"command": "private"}))
        return s.output

    s.invoker.run_streaming.side_effect = invoke
    session_id = await s.runner.spawn(s.request)
    result = await s.runner._active[session_id]
    assert result.tools_called == [{"name": s.binding.segment.tools[0]}]
    assert s.lifecycle.finish.call_args.args[2]["tools_summary"] == {s.binding.segment.tools[0]: 1}


async def test_private_path_output_is_withheld(setup):
    s = setup
    s.output = replace(s.output, text=s.binding.facade_config)
    s.invoker.run_streaming.return_value = s.output
    session_id = await s.runner.spawn(s.request)
    result = await s.runner._active[session_id]
    assert not result.success and result.output_text == ""
    assert result.error == "Peer result withheld"
    assert s.lifecycle.finish.call_args.args[2]["artifact_path"] is None


async def test_cancel_during_artifact_write_waits_for_effect_and_never_succeeds(
    db, setup, monkeypatch
):
    import threading

    from genesis.util import atomic

    s = setup
    started, released = threading.Event(), threading.Event()
    original = atomic.atomic_write_text

    def delayed(path, content):
        started.set()
        assert released.wait(3)
        original(path, content)

    monkeypatch.setattr(atomic, "atomic_write_text", delayed)
    session_id = await s.runner.spawn(s.request)
    task = s.runner._active[session_id]
    assert await asyncio.to_thread(started.wait, 3)
    s.runner.cancel(session_id)
    s.runner.cancel(session_id)
    await asyncio.sleep(0.01)
    assert not task.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = s.lifecycle.finish.call_args.args[2]
    assert not result["success"] and result["cancelled"]
    assert Path(result["artifact_path"]).read_text() == s.output.text
    assert (await cc_sessions.get_by_id(db, session_id))["status"] == "failed"


async def test_cancel_during_start_hook_settles_creation_and_owned_cleanup(db, setup):
    s = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(session_id, session_type, source):
        entered.set()
        await release.wait()

    s.manager.add_on_start(hold)
    spawn = asyncio.create_task(s.runner.spawn(s.request))
    await entered.wait()
    spawn.cancel()
    spawn.cancel()
    await asyncio.sleep(0)
    assert not spawn.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await spawn
    cursor = await db.execute("SELECT status FROM cc_sessions")
    assert [row[0] for row in await cursor.fetchall()] == ["failed"]
    s.invoker.run_streaming.assert_not_called()
    s.lifecycle.drain.assert_awaited_once()
    assert s.runner._semaphore._value == 2


@pytest.mark.parametrize("boundary", ["authorize", "queued", "begin"])
async def test_concurrent_cleanup_hold_fences_each_launch_boundary(setup, boundary):
    s = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(*args):
        entered.set()
        await release.wait()

    if boundary != "queued":
        getattr(s.lifecycle, boundary).side_effect = pause
        spawn = asyncio.create_task(s.runner.spawn(s.request))
        await entered.wait()
        s.runner._peer_cleanup_holds["prior"] = object()
        release.set()
        if boundary == "authorize":
            with pytest.raises(RuntimeError, match="reconciliation"):
                await spawn
            s.invoker.run_streaming.assert_not_called()
            return
        session_id = await spawn
    else:
        await s.runner._semaphore.acquire()
        await s.runner._semaphore.acquire()
        session_id = await s.runner.spawn(s.request)
        await asyncio.sleep(0)
        s.runner._peer_cleanup_holds["prior"] = object()
        s.runner._semaphore.release()
        s.runner._semaphore.release()
    result = await s.runner._active[session_id]
    assert not result.success
    s.invoker.run_streaming.assert_not_called()
    s.lifecycle.drain.assert_awaited_once()
    assert s.runner._semaphore._value == 2


async def test_creation_failure_retains_unknown_lineage_for_reconciliation(setup):
    s = setup
    s.manager.create_background = AsyncMock(side_effect=RuntimeError("controlled failure"))
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        await s.runner.spawn(s.request)
    assert s.runner._peer_cleanup_holds[s.binding.segment.segment_id].binding == s.binding
    s.lifecycle.drain.assert_awaited_once()
    s.invoker.run_streaming.assert_not_called()


async def test_scheduling_failure_drains_and_records_known_session(db, setup, monkeypatch):
    from genesis.peers import runner as peer_runner

    s = setup

    def fail(*args, **kwargs):
        raise RuntimeError("controlled scheduling failure")

    monkeypatch.setattr(peer_runner, "tracked_task", fail)
    with pytest.raises(RuntimeError, match="scheduling failure"):
        await s.runner.spawn(s.request)
    cursor = await db.execute("SELECT status FROM cc_sessions")
    assert [row[0] for row in await cursor.fetchall()] == ["failed"]
    s.lifecycle.drain.assert_awaited_once()
    assert not s.runner._active and s.runner._semaphore._value == 2


async def test_owner_roster_selects_provider_without_peer_override(setup, monkeypatch):
    import secrets

    from genesis.cc import roster

    s = setup
    monkeypatch.setenv("GENESIS_TEST_PROVIDER_AUTH", secrets.token_urlsafe(32))
    configured = {
        "default": "fixture_provider",
        "models": {
            "fixture_provider": {
                "anthropic_base_url": "https://fixture.invalid",
                "auth_env": "GENESIS_TEST_PROVIDER_AUTH",
                "model_id": "fixture-model",
            }
        },
    }

    async def invoke(invocation, on_event):
        routed, selected = roster.apply_active(invocation, configured)
        assert selected == "fixture_provider"
        assert routed.model_id_override == "fixture-model"
        assert routed.peer_segment is s.binding.segment
        assert routed.resume_session_id is None
        return s.output

    s.invoker.run_streaming.side_effect = invoke
    session_id = await s.runner.spawn(s.request)
    assert (await s.runner._active[session_id]).success
