"""Logical launch completion/cancel boundary; fake identities never reach signals."""

import asyncio
import os
from contextlib import suppress
from types import SimpleNamespace

import pytest

from genesis.mcp.local_modules import ModuleOwnershipError
from tests.test_mcp.test_local_module_owner import owner


@pytest.fixture
def logical_launch(monkeypatch):
    import genesis.mcp.local_modules as modules
    import genesis.util.proc_kill as processes

    created = asyncio.Event()
    process = SimpleNamespace(pid=1, returncode=None)
    calls = []

    def refuse(*args, **kwargs):
        raise AssertionError('Logical process can never reach a signal syscall')

    async def create(*args, **kwargs):
        assert args == ('logical-only',)
        # Cooperative immediate completion: no cancellation resistance.
        created.set()
        return process

    async def owner_confirm(proc, **kwargs):
        assert proc is process
        calls.append(('owner', kwargs))
        proc.returncode = 0

    async def helper_confirm(proc, **kwargs):
        assert proc is process
        calls.append(('helper', kwargs))
        proc.returncode = 0

    monkeypatch.setattr(os, 'kill', refuse)
    monkeypatch.setattr(os, 'killpg', refuse)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr(modules, 'confirm_process_exit', owner_confirm)
    monkeypatch.setattr(processes, 'confirm_process_exit', helper_confirm)
    return created, process, calls


@pytest.mark.parametrize('state', ['pending-expired', 'pending-future', 'completed-oracle'])
async def test_pending_launch_uses_recorded_resource_budget(logical_launch, state):
    created, process, calls = logical_launch
    scope = owner(cleanup_timeout_s=1)

    async def body():
        return await scope.spawn('logical-only', persistent=True)

    task = asyncio.create_task(scope.run(body))
    try:
        await asyncio.wait_for(created.wait(), 5)
        if state == 'completed-oracle':
            assert await task is process
        resource, = scope._resources
        assert resource.record.task.done()
        assert resource.launch.done() is (state == 'completed-oracle')
        deadline = asyncio.get_running_loop().time() + (.5 if state == 'pending-future' else -1)
        with suppress(ModuleOwnershipError, asyncio.CancelledError):
            await scope._retire_all([resource], deadline=deadline)
        await asyncio.gather(task, return_exceptions=True)
        assert len(calls) == 1 and calls[0][0] == 'owner', calls
        kwargs = calls[0][1]
        assert kwargs['terminate'] is (state == 'pending-future')
        assert 0 <= kwargs['confirm_timeout_s'] <= (.5 if state == 'pending-future' else 0)
        assert resource.record.confirmed and resource not in scope._resources
    finally:
        await scope.close()


@pytest.mark.parametrize('recorded', [False, True])
@pytest.mark.parametrize('ending', ['normal', 'cancel-success', 'cancel-uncertain', 'cancel-after-proof'])
async def test_recorded_launch_delegates_cleanup_but_default_launch_keeps_it(logical_launch, monkeypatch,
                                                                          recorded, ending):
    import genesis.util.proc_kill as processes

    created, process, calls = logical_launch
    record = processes.LaunchRecord() if recorded else None
    error = processes.ProcessCleanupUncertain('logical confirmation refusal')

    async def confirm(proc, **kwargs):
        assert proc is process
        calls.append(('helper', kwargs))
        if ending == 'cancel-uncertain':
            raise error
        proc.returncode = 0
        if ending == 'cancel-after-proof':
            raise asyncio.CancelledError

    monkeypatch.setattr(processes, 'confirm_process_exit', confirm)
    task = asyncio.create_task(processes.spawn_owned('logical-only', launch_record=record))
    await asyncio.wait_for(created.wait(), 5)
    assert not task.done()
    if ending != 'normal':
        task.cancel()
    result, = await asyncio.gather(task, return_exceptions=True)
    if ending == 'normal':
        assert result is process and calls == []
    elif recorded:
        assert isinstance(result, asyncio.CancelledError) and task.cancelled()
        assert calls == [], 'delegated cleanup acquired an independent policy'
    else:
        assert calls == [('helper', {})]
        if ending == 'cancel-uncertain':
            assert result is error and error.process is process and not task.cancelled()
        else:
            assert isinstance(result, asyncio.CancelledError) and task.cancelled()
    if recorded:
        assert record._claimed and record.task.done() and record.process is process
        assert not record.confirmed, 'only the delegated owner can certify physical settlement'
