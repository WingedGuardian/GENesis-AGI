"""Bridge cancellation, retention, admission and actual module-call wiring."""

import asyncio
from contextlib import nullcontext, suppress
from contextvars import ContextVar
from types import SimpleNamespace

import anyio
import pytest

from genesis.mcp.local_modules import (
    ModuleCleanupUnconfirmed,
    ModuleOperationStopped,
    ModuleOwnershipError,
)
from tests.test_mcp.test_local_module_owner import owned_children as owned_children
from tests.test_mcp.test_local_module_owner import owner


async def handled_cancel():
    asyncio.current_task().cancel('handled operation history')
    with suppress(asyncio.CancelledError):
        await asyncio.sleep(0)


@pytest.mark.parametrize('parent_history', [False, True])
@pytest.mark.parametrize('body_history', [False, True])
@pytest.mark.parametrize('ending', ['owner', 'caller', 'caller-owner', 'owner-caller'])
async def test_operation_cancellation_provenance(parent_history, body_history, ending):
    scope = owner(cleanup_timeout_s=1)
    entered = asyncio.Event()
    handles = {}

    async def body():
        handles['child'] = asyncio.current_task()
        if body_history:
            await handled_cancel()
        entered.set()
        await asyncio.Event().wait()

    async def caller():
        if parent_history:
            await handled_cancel()
        await scope.run(body)

    task = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for action in ending.split('-'):
            if action == 'caller':
                task.cancel()
            else:
                scope._units[handles['child']].cancel_scope.cancel()
        result, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        if 'caller' in ending:
            assert isinstance(result, asyncio.CancelledError) and task.cancelled()
        else:
            assert isinstance(result, ModuleOperationStopped) and not task.cancelled()
        assert not scope._units
        await scope.close()
        assert scope.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await scope.close()


@pytest.mark.parametrize('ending', ['result', 'error', 'internal-cancel'])
async def test_operation_keeps_results_and_errors(ending):
    scope = owner()
    result = object()
    error = ValueError('module failure')

    async def body():
        scope.require_unit()
        if ending == 'error':
            raise error
        if ending == 'internal-cancel':
            raise asyncio.CancelledError
        return result

    try:
        if ending == 'result':
            assert await scope.run(body) is result
        else:
            with pytest.raises(ValueError if ending == 'error' else ModuleOperationStopped) as caught:
                await scope.run(body)
            if ending == 'error':
                assert caught.value is error
        assert not scope._units
    finally:
        await scope.close()


@pytest.mark.parametrize('ending', ['cancel', 'close'])
async def test_scheduled_operation_cannot_acquire_before_admission(ending, monkeypatch):
    scope = owner()
    calls = []

    async def body():
        calls.append('acquired')

    if ending == 'cancel':
        import genesis.mcp.local_modules as local_modules

        original = local_modules._task
        scheduled = asyncio.Event()
        first = True

        def defer_entry(coro):
            nonlocal first
            if not first:
                return original(coro)
            first = False

            async def before_entry():
                try:
                    scheduled.set()
                    await asyncio.Event().wait()
                finally:
                    coro.close()  # invoke never entered, no resource exists
            return original(before_entry())

        monkeypatch.setattr(local_modules, '_task', defer_entry)
        task = asyncio.create_task(scope.run(body))
        await asyncio.wait_for(scheduled.wait(), 5)
        task.cancel()
    else:
        task = asyncio.create_task(scope.run(body))
        # Close after the caller queues its child but before child admission.
        asyncio.get_running_loop().call_soon(scope.begin_draining)
    result, = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError if ending == 'cancel' else ModuleOwnershipError)
    assert calls == [] and not scope._units and not scope._resources
    await scope.close()
    assert scope.closed


@pytest.mark.parametrize('existing', ['unit', 'resource', 'global', 'expired'])
async def test_caller_cancellation_publishes_earliest_child_deadline(existing):
    scope = owner(cleanup_timeout_s=1)
    entered = asyncio.Event()
    handles = {}
    expected = asyncio.get_running_loop().time() + (0 if existing == 'expired' else .5)

    async def body():
        child = asyncio.current_task()
        handles['child'] = child
        if existing in {'unit', 'expired'}:
            scope._unit_deadlines[child] = expected
        if existing == 'resource':
            class Client:
                async def aclose(self):
                    pass
            scope.retain_client(Client())
            next(iter(scope._resources)).deadline = expected
        if existing == 'global':
            scope._deadline = expected
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            handles['deadline'] = scope._unit_deadlines.get(child)

    task = asyncio.create_task(scope.run(body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert handles['deadline'] == expected
        assert not scope._units
    finally:
        await scope.close()


@pytest.mark.parametrize('has_resource', [False, True])
async def test_pending_operation_quarantines_until_actual_completion(has_resource):
    scope = owner(cleanup_timeout_s=.03)
    entered, release = asyncio.Event(), asyncio.Event()
    handles, events = {}, []

    class Client:
        async def aclose(self):
            events.append('closed')

    async def body():
        handles['child'] = asyncio.current_task()
        if has_resource:
            scope.retain_client(Client())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            with anyio.CancelScope(shield=True):
                await release.wait()
            events.append('left')

    task = asyncio.create_task(scope.run(body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(ModuleOwnershipError, match='unconfirmed'):
            await task
        assert handles['child'] in scope._units
        with pytest.raises(ModuleOwnershipError, match='unconfirmed'):
            await scope.run(lambda: asyncio.sleep(0))
        assert not scope.closed
        release.set()
        await asyncio.gather(handles['child'], return_exceptions=True)
        # A fully completed zero-resource child removes its unit uncertainty;
        # an uncertain client is retained until the owner confirms its closure.
        if not has_resource:
            assert await scope.run(lambda: asyncio.sleep(0, result='fresh')) == 'fresh'
        await scope.close()
        assert scope.closed and events == (['left', 'closed'] if has_resource else ['left'])
    finally:
        release.set()
        await asyncio.gather(task, *([handles['child']] if 'child' in handles else []), return_exceptions=True)
        await scope.close()


@pytest.mark.parametrize('kind', ['client', 'process'])
async def test_repeated_caller_cancellation_retains_resource_and_allowance(kind, owned_children):
    import os
    import sys

    scope = owner(cleanup_timeout_s=1)
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handles, events = {}, []

    class Client:
        async def aclose(self):
            assert events == ['callback-left']
            events.append('closed')

    async def body():
        handles['child'] = asyncio.current_task()
        client = Client()
        if kind == 'client':
            scope.retain_client(client)
        else:
            handles['process'] = await scope.spawn(sys.executable, '-I', '-S', '-u', '-c',
                                                  'import time;time.sleep(300)')
        with scope.use(client) if kind == 'client' else nullcontext():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                handles['deadline'] = scope._unit_deadlines[asyncio.current_task()]
                with anyio.CancelScope(shield=True):
                    cleanup.set()
                    await release.wait()
                events.append('callback-left')

    task = asyncio.create_task(scope.run(body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cleanup.wait(), 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert scope._unit_deadlines[handles['child']] == handles['deadline']
        if kind == 'client':
            assert next(iter(scope._resources)).users == {handles['child']}
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and not scope._units
        await scope.close()
        assert scope.closed
        if kind == 'process':
            proc = handles['process']
            assert proc.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(proc.pid, 0)
        else:
            assert events == ['callback-left', 'closed']
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await scope.close()


@pytest.mark.parametrize('owned', [False, True])
async def test_actual_module_call_runs_readiness_and_operation_in_one_context(monkeypatch, owned):
    from genesis.mcp.health import module_ops

    scope = owner()
    parent = asyncio.current_task()
    marker = ContextVar('module-call-test')
    token = marker.set(object())
    expected = marker.get()
    observed = []

    async def start(adapter):
        observed.append(asyncio.current_task())
        assert marker.get() is expected
        if owned:
            scope.require_unit()

    async def execute(operation, params):
        observed.append(asyncio.current_task())
        assert marker.get() is expected
        return {'operation': operation, 'params': params}

    adapter = SimpleNamespace(enabled=True, execute_operation=execute)
    monkeypatch.setattr(module_ops, '_get_adapters', lambda: {'fixture': adapter})
    monkeypatch.setattr(module_ops, '_resource_owner', scope if owned else None)
    monkeypatch.setattr(module_ops, '_ensure_adapter_started', start)
    try:
        assert await module_ops._impl_module_call('fixture', ' echo ', {'x': 1}) == {
            'operation': 'echo', 'params': {'x': 1}}
        assert len(observed) == 2 and observed[0] is observed[1]
        assert (observed[0] is parent) is not owned
        assert not scope._units
    finally:
        marker.reset(token)
        await scope.close()


@pytest.mark.parametrize('failure', ['ordinary', 'ownership', 'uncertain'])
async def test_operation_ownership_error_has_priority_over_caller_cancel(failure):
    scope = owner()
    entered = asyncio.Event()
    error = {'ordinary': ValueError('ordinary operation failure'),
             'ownership': ModuleOwnershipError('admission refused'),
             'uncertain': ModuleCleanupUnconfirmed('unconfirmed operation')}[failure]
    handles = {}

    async def body():
        handles['child'] = asyncio.current_task()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise error

    task = asyncio.create_task(scope.run(body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        if failure == 'uncertain':
            with pytest.raises(ModuleOwnershipError) as caught:
                await task
            assert caught.value is error and not task.cancelled()
        else:
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled()
        assert handles['child'].done() and handles['child'].exception() is error
        assert not scope._units
    finally:
        await scope.close()


async def test_terminal_admission_refusal_does_not_replace_caller_cancellation(monkeypatch):
    import genesis.mcp.local_modules as local_modules

    scope = owner()
    await scope.close()
    original = local_modules._task
    first = True
    calls = []

    def cancel_caller_at_completion(coro):
        nonlocal first
        child = original(coro)
        if first:
            first = False
            caller = asyncio.current_task()
            child.add_done_callback(lambda _: caller.cancel('terminal admission race'))
        return child

    async def body():
        calls.append('acquired')

    monkeypatch.setattr(local_modules, '_task', cancel_caller_at_completion)
    task = asyncio.create_task(scope.run(body))
    result, = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError) and task.cancelled()
    assert calls == [] and scope.closed and not scope._units and not scope._resources
