"""Every durable execution writer uses the installed transaction-bound host gate."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from genesis.cc.exceptions import CCRateLimitError
from genesis.peers.tasks import TaskRefusal
from genesis.runtime.init.peers import PeerRuntime
from tests.test_peers import test_lifecycle_state as lifecycle_tests
from tests.test_peers import test_provider_state as provider_tests

lifecycle = lifecycle_tests.lifecycle


def owner(registry, directory):
    runtime = SimpleNamespace(
        is_bootstrapped=True,
        paused=False,
        _direct_session_runner=SimpleNamespace(_peer_cleanup_holds={}),
    )
    service = PeerRuntime(runtime, registry, directory)
    service.recovered = True
    service.coordinator = SimpleNamespace(broker=SimpleNamespace(_runner=object()))
    service.poll = SimpleNamespace(done=lambda: False)
    return service


async def snapshot(registry):
    fields = {
        "peer_tasks": "state,generation,queue_id,slot_reserved,work_elapsed_s",
        "direct_session_queue": "id,status,session_id",
        "approval_requests": "id,status,consumed_at",
        "peer_task_consents": "approval_id",
        "peer_segments": "id,status,started_at,session_id",
        "cc_rate_limit_parks": "id,status,attempts,claimed_at",
    }
    async with registry.connection() as db:
        return {
            name: [
                tuple(row)
                for row in await (
                    await db.execute("SELECT " + columns + " FROM " + name + " ORDER BY 1")
                ).fetchall()
            ]
            for name, columns in fields.items()
        }


async def exercise(registry, directory, target, invoke, blocked):
    service = owner(registry, directory)
    await registry.configure("fallback", service_url="https://fixture.invalid/v1/agent/a2a")
    if blocked == "disabled":
        await registry.configure("disabled")
    elif blocked == "paused":
        service.runtime.paused = True
    calls = 0

    async def gate(db):
        nonlocal calls
        calls += 1
        if blocked == "pause_after_write" and calls == 2:
            service.runtime.paused = True
        await service.require_execution(db)

    target.execution_gate = gate
    before = await snapshot(registry)
    with pytest.raises(TaskRefusal):
        await invoke()
    assert await snapshot(registry) == before
    service.runtime.paused = False
    await registry.configure("fallback", service_url="https://fixture.invalid/v1/agent/a2a")
    target.execution_gate = service.require_execution
    assert await invoke()  # Enabled positive control reaches the durable effect.
    assert await snapshot(registry) != before


@pytest.mark.parametrize("writer", ["claim", "approval"])
@pytest.mark.parametrize("blocked", ["disabled", "paused", "pause_after_write"])
async def test_claim_and_approval_writer_gates(lifecycle, writer, blocked):
    s = lifecycle
    approval = await lifecycle_tests.hold(s)
    await s.state.settle(s.binding, 0, clean=True)
    assert await s.gate.resolve_request(approval, decision="approved", resolved_by="dashboard")
    if writer == "claim":
        assert await s.state.resume_approval(s.task["id"])
        invoke = s.state.claim
    else:

        async def invoke():
            return await s.state.resume_approval(s.task["id"])

    await exercise(s.registry, s.state.directory, s.state, invoke, blocked)


@pytest.mark.parametrize("blocked", ["disabled", "paused", "pause_after_write"])
async def test_provider_writer_gates(tmp_path, monkeypatch, blocked):
    monkeypatch.setattr(provider_tests.config, "effective_mode", lambda: "live")
    monkeypatch.setattr(
        provider_tests.config, "load_config", lambda: dict(provider_tests.config.DEFAULTS)
    )
    registry, task, state, provider, claim = await provider_tests.fixture(tmp_path)
    binding = await claim()
    assert await provider.park(binding, CCRateLimitError("Fixture interruption"))
    await state.settle(binding, 0, clean=True)
    async with registry.connection() as db:
        identifier = (await (await db.execute("SELECT id FROM cc_rate_limit_parks")).fetchone())[0]

    async def invoke():
        return await provider.resume(identifier, now=provider_tests.utcnow() + timedelta(hours=6))

    await exercise(registry, state.directory, provider, invoke, blocked)
