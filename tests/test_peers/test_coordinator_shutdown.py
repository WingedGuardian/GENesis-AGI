"""Barrier controls for dispatch ownership during coordinator shutdown."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from genesis.cc.direct_session import DirectSessionRunner
from genesis.cc.peer_segment import PeerSegment
from genesis.peers.coordinator import PeerCoordinator
from genesis.peers.runner import PeerRunState, _lifecycle
from genesis.peers.session import PeerSessionBinding


@pytest.fixture
def coordinator(tmp_path):
    value = PeerCoordinator.__new__(PeerCoordinator)
    value._dispatch_lock = asyncio.Lock()
    value._stopping = False
    value._bindings, value._sessions, value._notifications = {}, {}, {}
    value._retire_invalid_pending = AsyncMock()
    value._finish_unstarted = AsyncMock()
    value.state = SimpleNamespace(
        claim=AsyncMock(return_value=None), consent=AsyncMock(return_value=True)
    )
    value.broker = SimpleNamespace(
        _runner=object(), issue=AsyncMock(), invalidate=Mock(), close=AsyncMock()
    )
    value.runner = SimpleNamespace(
        _active={}, _peer_runs={}, _peer_cleanup_holds={}, spawn=AsyncMock()
    )
    value.runner.cancel = lambda sid: DirectSessionRunner.cancel(value.runner, sid)
    binding = PeerSessionBinding(
        uuid4().hex,
        PeerSegment(uuid4().hex, time.time() + 60, ("mcp__genesis_peer__task_context",)),
        0,
        str(tmp_path / "segment" / "facade.json"),
        str(tmp_path / "segment"),
    )
    value._binding = lambda row: binding
    return value


async def test_queued_dispatch_refuses_after_close_without_touching_state(coordinator):
    async with coordinator._dispatch_lock:
        dispatch = asyncio.create_task(coordinator.dispatch_one())
        await asyncio.sleep(0)
        close = asyncio.create_task(coordinator.close())
        await asyncio.sleep(0)
        assert coordinator._stopping
    with pytest.raises(RuntimeError, match="unavailable"):
        await dispatch
    await close
    coordinator._retire_invalid_pending.assert_not_awaited()
    coordinator.state.claim.assert_not_awaited()
    coordinator.runner.spawn.assert_not_awaited()


async def test_close_waits_for_delayed_spawn_and_owned_cleanup(coordinator):
    coordinator.state.claim.return_value = {
        "id": uuid4().hex,
        "segment_id": uuid4().hex,
        "reserved_s": 30,
        "message_json": "{}",
        "work_limit_s": 30,
    }
    spawn_entered, release_spawn = asyncio.Event(), asyncio.Event()
    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
    execution_started = asyncio.Event()
    events = []

    async def session():
        execution_started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            events.append("cleanup")

    async def spawn(request):
        spawn_entered.set()
        await release_spawn.wait()
        coordinator.runner._active["session"] = asyncio.create_task(session())
        await execution_started.wait()
        return "session"

    async def broker_close():
        assert coordinator.runner._active["session"].done()
        events.append("broker")

    coordinator.runner.spawn.side_effect = spawn
    coordinator.broker.close.side_effect = broker_close
    dispatch = asyncio.create_task(coordinator.dispatch_one())
    await asyncio.wait_for(spawn_entered.wait(), 2)
    close = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    coordinator.broker.close.assert_not_awaited()
    release_spawn.set()
    assert await dispatch == "session"
    await asyncio.wait_for(cleanup_started.wait(), 2)
    assert not close.done()
    coordinator.broker.close.assert_not_awaited()
    release_cleanup.set()
    await close
    assert events == ["cleanup", "broker"]
    coordinator.broker.invalidate.assert_called_once()


async def test_cancelled_close_waiter_can_be_retried(coordinator):
    async with coordinator._dispatch_lock:
        first = asyncio.create_task(coordinator.close())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert coordinator._stopping
        coordinator.broker.close.assert_not_awaited()
    await coordinator.close()
    assert not coordinator._dispatch_lock.locked()
    coordinator.broker.close.assert_awaited_once()


@pytest.mark.parametrize("uncertain", [False, True])
async def test_failed_spawn_settles_before_shutdown(coordinator, uncertain):
    coordinator.state.claim.return_value = {
        "id": uuid4().hex,
        "segment_id": uuid4().hex,
        "reserved_s": 30,
        "message_json": "{}",
        "work_limit_s": 30,
    }
    spawn_entered, release_spawn = asyncio.Event(), asyncio.Event()
    settled = []

    async def spawn(request):
        spawn_entered.set()
        await release_spawn.wait()
        raise RuntimeError("Fixture spawn failed")

    async def finish(binding):
        settled.append(True)
        if uncertain:
            coordinator.runner._peer_cleanup_holds["unknown"] = binding

    async def broker_close():
        assert settled == [True]

    coordinator.runner.spawn.side_effect = spawn
    coordinator._finish_unstarted = finish
    coordinator.broker.close.side_effect = broker_close
    dispatch = asyncio.create_task(coordinator.dispatch_one())
    await asyncio.wait_for(spawn_entered.wait(), 2)
    close = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    coordinator.broker.close.assert_not_awaited()
    release_spawn.set()
    with pytest.raises(RuntimeError, match="Fixture spawn failed"):
        await dispatch
    if uncertain:
        with pytest.raises(RuntimeError, match="reconciliation"):
            await close
        coordinator.broker.close.assert_not_awaited()
    else:
        await close
        coordinator.broker.close.assert_awaited_once()


async def test_no_claim_dispatch_and_repeated_close(coordinator):
    assert await coordinator.dispatch_one() is None
    coordinator.state.claim.assert_awaited_once()
    coordinator.runner.spawn.assert_not_awaited()
    await coordinator.close()
    await coordinator.close()
    with pytest.raises(RuntimeError, match="unavailable"):
        await coordinator.dispatch_one()
    assert coordinator.broker.close.await_count == 2


@pytest.mark.parametrize(
    "failure", [False, RuntimeError("Fixture drain failure"), asyncio.CancelledError()]
)
async def test_unstarted_cleanup_failure_fences_all_launches_and_close(
    coordinator, monkeypatch, failure
):
    binding = coordinator._binding({})
    coordinator.runner._rt = SimpleNamespace(_peer_session_lifecycle=coordinator)
    coordinator.state.settle = AsyncMock()
    drain = AsyncMock(return_value=False) if failure is False else AsyncMock(side_effect=failure)
    monkeypatch.setattr("genesis.peers.coordinator._drain", drain)
    await PeerCoordinator._finish_unstarted(coordinator, binding)
    held = coordinator.runner._peer_cleanup_holds[binding.segment.segment_id]
    assert held.binding is binding
    assert coordinator.state.settle.call_args.kwargs == {"clean": False, "uncertain": True}
    with pytest.raises(RuntimeError, match="reconciliation"):
        _lifecycle(coordinator.runner)
    with pytest.raises(RuntimeError, match="reconciliation"):
        await coordinator.close()
    coordinator.broker.close.assert_not_awaited()


async def test_unstarted_failed_drain_preserves_existing_hold(coordinator, monkeypatch):
    binding = coordinator._binding({})
    prior = PeerRunState(binding)
    coordinator.runner._peer_cleanup_holds[binding.segment.segment_id] = prior
    coordinator.state.settle = AsyncMock()
    monkeypatch.setattr("genesis.peers.coordinator._drain", AsyncMock(return_value=False))
    await PeerCoordinator._finish_unstarted(coordinator, binding)
    assert coordinator.runner._peer_cleanup_holds[binding.segment.segment_id] is prior
