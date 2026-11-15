"""Cancellation ownership contracts for local module operation boundaries."""

import asyncio
import os
import sys
from contextlib import suppress

import pytest

from tests.test_mcp.test_local_module_owner import owned_children as owned_children
from tests.test_mcp.test_local_module_owner import owner


@pytest.mark.parametrize('kind', ['process', 'client'])
@pytest.mark.asyncio
async def test_level_cancellation_settles_registered_callback_resources(kind, owned_children):
    import anyio

    scope = owner()
    events = []
    proc = None

    class Client:
        async def aclose(self):
            await anyio.sleep(0)  # cancellation-cooperative physical closer
            events.append('client-closed')

    client = Client()
    try:
        with anyio.CancelScope() as cancelling:
            async with scope.unit():
                if kind == 'process':
                    proc = await scope.spawn(sys.executable, '-I', '-S', '-u', '-c',
                                             'import time;time.sleep(300)')
                else:
                    scope.retain_client(client)
                try:
                    if kind == 'client':
                        with scope.use(client):
                            cancelling.cancel()
                            await anyio.sleep(0)
                    else:
                        cancelling.cancel()
                        await anyio.sleep(0)
                finally:
                    events.append('callback-left')
        await scope.close()
        assert events == (['callback-left', 'client-closed'] if kind == 'client' else ['callback-left'])
        assert scope.closed
        if proc is not None:
            assert proc.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(proc.pid, 0)
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_owner_stop_does_not_cancel_its_callers_task_group():
    import anyio

    from genesis.mcp.local_modules import ModuleOwnershipError

    scope = owner()
    entered, sibling_ready, release = anyio.Event(), anyio.Event(), anyio.Event()
    facts = {'stopped': False, 'sibling_cancelled': False, 'parent_continued': False}

    async def operation():
        try:
            async with scope.unit():
                entered.set()
                await anyio.sleep_forever()
        except ModuleOwnershipError:
            facts['stopped'] = True

    async def sibling():
        sibling_ready.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            facts['sibling_cancelled'] = True
            raise

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(operation)
            group.start_soon(sibling)
            await entered.wait()
            await sibling_ready.wait()
            await scope.close()
            facts['parent_continued'] = True
            release.set()
        assert facts == {'stopped': True, 'sibling_cancelled': False, 'parent_continued': True}
        assert scope.closed
    finally:
        release.set()
        await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('baseline', [0, 1])
@pytest.mark.parametrize('handled', [False, True])
@pytest.mark.parametrize('mode', ['complete', 'owner', 'native', 'native-owner', 'owner-native'])
async def test_operation_scope_preserves_external_cancellation(baseline, handled, mode):
    from genesis.mcp.local_modules import ModuleOperationStopped

    scope = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    facts, handles = {}, {}

    async def body():
        handles['child'] = asyncio.current_task()
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if not handled:
                raise

    async def operation():
        task = asyncio.current_task()
        if baseline:
            task.cancel('previously handled cancellation')
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0)
        assert task.cancelling() == baseline
        try:
            await scope.run(body)
            facts['outcome'] = 'complete'
        except ModuleOperationStopped:
            facts['outcome'] = 'owner'
        except asyncio.CancelledError:
            facts['outcome'] = 'native'
            raise

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if mode == 'complete':
            release.set()
        else:
            # Drive the actual registered unit scope and native caller in one
            # event-loop turn. The SDK tests separately drive owner.close().
            for action in mode.split('-'):
                if action == 'owner':
                    scope._units[handles['child']].cancel_scope.cancel()
                else:
                    task.cancel('external caller')
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        expected = 'native' if 'native' in mode else 'owner' if 'owner' in mode else 'complete'
        assert facts == {'outcome': expected}
        assert task.cancelled() is (expected == 'native')
        assert not scope._units
        await scope.close()
        assert scope.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('mode', ['parent', 'parent-owner', 'owner-parent'])
async def test_operation_scope_leaves_sdk_parent_cancellation_to_parent(nested, mode):
    import anyio

    from genesis.mcp.local_modules import ModuleOperationStopped

    scope = owner()
    entered = asyncio.Event()
    handles, facts = {}, {}

    async def body():
        handles['child'] = asyncio.current_task()
        outer = scope._units[asyncio.current_task()].cancel_scope
        async def held():
            assert scope._units[asyncio.current_task()].cancel_scope is outer
            entered.set()
            await asyncio.Event().wait()
        if nested:
            await scope.run(held)
        else:
            await held()

    async def operation():
        try:
            with anyio.CancelScope() as parent:
                handles['parent'] = parent
                await scope.run(body)
                facts['silently_completed'] = True
            facts['parent_caught'] = parent.cancelled_caught
        except ModuleOperationStopped:
            facts['misclassified_as_owner'] = True

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for action in mode.split('-'):
            if action == 'owner':
                scope._units[handles['child']].cancel_scope.cancel()
            else:
                handles['parent'].cancel()
        await asyncio.wait_for(task, 5)
        assert facts == {'parent_caught': True}
        assert not scope._units
        await scope.close()
        assert scope.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
async def test_operation_scope_child_with_copied_context_has_its_own_unit():
    from genesis.mcp.local_modules import ModuleOperationStopped, ModuleOwnershipError

    scope = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    handles, facts = {}, {}

    async def child():
        with pytest.raises(ModuleOwnershipError):
            scope.require_unit()
        async with scope.unit():
            handles['child_scope'] = scope._units[asyncio.current_task()].cancel_scope
            entered.set()
            await release.wait()
        facts['child_completed'] = True

    async def parent():
        try:
            async with scope.unit():
                handles['parent_scope'] = scope._units[asyncio.current_task()].cancel_scope
                handles['child'] = asyncio.create_task(child())
                await asyncio.Event().wait()
        except ModuleOperationStopped:
            facts['parent_stopped'] = True

    task = asyncio.create_task(parent())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert handles['parent_scope'] is not handles['child_scope']
        handles['parent_scope'].cancel()
        await asyncio.wait_for(task, 5)
        assert facts == {'parent_stopped': True}
        assert not handles['child'].done()
        release.set()
        await asyncio.wait_for(handles['child'], 5)
        assert facts == {'parent_stopped': True, 'child_completed': True}
        await scope.close()
        assert scope.closed and not scope._units
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, *([handles['child']] if 'child' in handles else []), return_exceptions=True)
        await scope.close()
