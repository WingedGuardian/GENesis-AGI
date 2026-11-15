"""Observable local-process/reply/closure contracts for the opt-in owner."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from contextlib import suppress

import pytest

from genesis.modules.external.config import IPCConfig
from genesis.modules.external.ipc import create_ipc_adapter

CHILD = """
import json,os,sys,time
from pathlib import Path
ready,release=map(Path,sys.argv[1:])
for line in sys.stdin:
    request=json.loads(line)
    if request['path']=='EOF':sys.exit(0)
    if request['path']=='invalid':
        print('not-json',flush=True)
        continue
    if request['path']=='business-error':
        print(json.dumps({'error':'rejected','pid':os.getpid()}),flush=True)
        continue
    if request['path']=='held':
        ready.write_text(str(os.getpid()))
        while not release.exists(): time.sleep(.01)
    print(json.dumps({'path':request['path'],'pid':os.getpid(),
                      'label':os.environ.get('GENESIS_CALLER_PROFILE')}),flush=True)
"""


@pytest.fixture
def owned_children(monkeypatch):
    actual = subprocess.Popen
    children = []

    class OwnedOnly(actual):
        def __init__(self, argv, *args, **kwargs):
            assert list(argv[:5]) == [sys.executable, '-I', '-S', '-u', '-c']
            super().__init__(argv, *args, **kwargs)
            children.append(self)

    def refuse(*args, **kwargs):
        raise AssertionError('No network is allowed in owned-process tests')

    monkeypatch.setattr(subprocess, 'Popen', OwnedOnly)
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)
    yield children
    for proc in children:
        if proc.poll() is None:
            with suppress(ProcessLookupError):
                if os.getpgid(proc.pid) == proc.pid:
                    os.killpg(proc.pid, 9)
                else:
                    proc.kill()
        proc.wait(timeout=5)


def owner(**kwargs):
    from genesis.mcp.local_modules import ModuleResourceOwner

    return ModuleResourceOwner(grace_s=0, **kwargs)


def stdio(tmp_path, scope):
    return create_ipc_adapter(IPCConfig(method='stdio', command=[
        sys.executable, '-I', '-S', '-u', '-c', CHILD,
        str(tmp_path/'ready'), str(tmp_path/'release'),
    ], timeout=2), owner=scope)


async def ready(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.01)


async def exchange(scope, adapter, path):
    async with scope.unit():
        if adapter.needs_start:
            await adapter.start()
        return await adapter.send(path)


@pytest.mark.asyncio
async def test_cancelled_stdio_reply_never_becomes_next_reply(tmp_path, owned_children):
    scope = owner()
    adapter = stdio(tmp_path, scope)
    first = asyncio.create_task(exchange(scope, adapter, 'held'))
    try:
        await ready(tmp_path/'ready')
        old_pid = int((tmp_path/'ready').read_text())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 5)
        (tmp_path/'release').touch()
        second = await exchange(scope, adapter, 'second')
        assert second['path'] == 'second'
        assert second['pid'] != old_pid
        assert owned_children[0].poll() is not None
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
async def test_stdio_concurrent_waiter_cancel_preserves_active_exchange(tmp_path, owned_children):
    scope = owner()
    adapter = stdio(tmp_path, scope)
    first = asyncio.create_task(exchange(scope, adapter, 'held'))
    second = None
    try:
        await ready(tmp_path/'ready')
        second = asyncio.create_task(exchange(scope, adapter, 'second'))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert owned_children[0].poll() is None
        (tmp_path/'release').touch()
        result = await first
        third = await exchange(scope, adapter, 'third')
        assert result['path'] == 'held' and third['path'] == 'third'
        assert result['pid'] == third['pid']
        assert len(owned_children) == 1
    finally:
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
async def test_stdio_parallel_calls_have_their_own_replies(tmp_path, owned_children):
    scope = owner()
    adapter = stdio(tmp_path, scope)
    try:
        results = await asyncio.gather(*(exchange(scope, adapter, str(i)) for i in range(8)))
        assert [r['path'] for r in results] == [str(i) for i in range(8)]
        assert len({r['pid'] for r in results}) == len(owned_children) == 1
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_stdio_spawn_uses_fresh_environment_without_client_label(tmp_path, monkeypatch, owned_children):
    scope = owner()
    adapter = stdio(tmp_path, scope)
    monkeypatch.setenv('GENESIS_CALLER_PROFILE', 'codex')
    try:
        result = await exchange(scope, adapter, 'one')
        assert result['label'] is None
    finally:
        await scope.close()


@pytest.mark.parametrize('path', ['invalid', 'EOF', 'held'])
@pytest.mark.asyncio
async def test_ambiguous_stdio_result_retires_old_process(tmp_path, path, owned_children):
    scope = owner()
    adapter = stdio(tmp_path, scope)
    try:
        first = await exchange(scope, adapter, 'normal')
        normal_timeout = adapter._timeout
        adapter._timeout = .25
        result = await exchange(scope, adapter, path)
        assert 'error' in result
        # The short fault trigger must not time a fresh program's normal startup.
        adapter._timeout = normal_timeout
        next_reply = await exchange(scope, adapter, 'next')
        assert next_reply.get('path') == 'next' and next_reply['pid'] != first['pid'], next_reply
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_well_formed_business_error_does_not_retire_stdio(tmp_path, owned_children):
    scope = owner()
    adapter = stdio(tmp_path, scope)
    try:
        rejected = await exchange(scope, adapter, 'business-error')
        next_reply = await exchange(scope, adapter, 'next')
        assert rejected['error'] == 'rejected' and rejected['pid'] == next_reply['pid']
        assert len(owned_children) == 1
    finally:
        await scope.close()


@pytest.mark.parametrize('method', ['SHELL', 'CC'])
@pytest.mark.parametrize('ending', ['normal', 'cancel'])
@pytest.mark.asyncio
async def test_actual_ssh_methods_own_local_group(tmp_path, method, ending, monkeypatch, owned_children):
    scope = owner()
    actual_create = asyncio.create_subprocess_exec
    script = ("import json,os,sys,time;from pathlib import Path;"
              "Path(sys.argv[1]).write_text(str(os.getpid()));"
              + ("time.sleep(300)" if ending == 'cancel' else
                 "print(json.dumps({'type':'result','result':'owned'}),flush=True)"))

    async def substitute(*argv, **kwargs):
        assert argv[0] == 'ssh' and kwargs['start_new_session'] is True
        return await actual_create(sys.executable, '-I', '-S', '-u', '-c', script,
                                   str(tmp_path/'ssh-ready'), **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', substitute)
    adapter = create_ipc_adapter(IPCConfig(method='ssh', ssh_host='owned.invalid'), owner=scope)

    async def invoke():
        async with scope.unit():
            return await adapter.send('owned-command', {'prompt': 'owned-prompt'}, method)

    task = asyncio.create_task(invoke())
    try:
        await ready(tmp_path/'ssh-ready')
        if ending == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        else:
            result = await task
            assert 'owned' in json.dumps(result)
        assert len(owned_children) == 1 and owned_children[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(owned_children[0].pid, 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
async def test_eof_drain_cancels_held_stdio_and_refuses_new_work(tmp_path, owned_children):
    from genesis.mcp.local_modules import ModuleOperationStopped

    scope = owner()
    adapter = stdio(tmp_path, scope)
    task = asyncio.create_task(exchange(scope, adapter, 'held'))
    try:
        await ready(tmp_path/'ready')
        scope.begin_draining()
        await asyncio.wait_for(scope.close(), 5)
        assert task.done() and not task.cancelled()
        assert isinstance(task.exception(), ModuleOperationStopped)
        assert owned_children[0].poll() is not None
        with pytest.raises(RuntimeError, match='closing|closed|draining'):
            async with scope.unit():
                pytest.fail('closed owner admitted work')
        await scope.close()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_http_close_is_not_certified_by_closed_flag():
    scope = owner()

    class Client:
        is_closed = False
        calls = 0

        async def aclose(self):
            self.calls += 1
            self.is_closed = True
            raise OSError('transport did not finish closing')

    client = Client()
    async with scope.unit():
        scope.retain_client(client)
    with pytest.raises(RuntimeError, match='unconfirmed'):
        await scope.close()
    assert client.is_closed and client.calls == 1
    with pytest.raises(RuntimeError):
        async with scope.unit():
            pytest.fail('uncertain close admitted work')
    with pytest.raises(RuntimeError, match='unconfirmed'):
        await scope.close()
    assert client.calls == 1, 'a closed-flag no-op must not certify an earlier failed close'


@pytest.mark.asyncio
async def test_grace_preserves_accepted_nested_unit_but_refuses_new_admission():
    from genesis.mcp.local_modules import ModuleResourceOwner

    scope = ModuleResourceOwner(grace_s=1)

    async def begin():
        scope.begin_draining()

    async with scope.unit():
        await asyncio.create_task(begin())
        async with scope.unit():
            assert scope.require_unit() is asyncio.current_task()
        with pytest.raises(RuntimeError):
            await asyncio.create_task(exchange(scope, None, 'must-not-run'))
    await scope.close()
    assert scope.closed


@pytest.mark.asyncio
async def test_uncooperative_close_returns_unknown_without_cancelling_resource():
    scope = owner(cleanup_timeout_s=.03)
    entered, release = asyncio.Event(), asyncio.Event()

    class Client:
        async def aclose(self):
            entered.set()
            await release.wait()

    client = Client()
    async with scope.unit():
        scope.retain_client(client)
    closing = asyncio.create_task(scope.close())
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await asyncio.wait_for(closing, 5)
        with pytest.raises(RuntimeError):
            async with scope.unit():
                pytest.fail('unknown resource admitted work')
    finally:
        release.set()
        await asyncio.sleep(0)
        await scope.close()


@pytest.mark.asyncio
async def test_launch_record_reuse_is_refused_before_another_child(owned_children):
    from genesis.util.proc_kill import LaunchRecord, spawn_owned

    record = LaunchRecord()
    argv = [sys.executable, '-I', '-S', '-u', '-c', 'pass']
    proc = await spawn_owned(*argv, start_new_session=True, launch_record=record)
    await proc.wait()
    assert record.task.done()
    with pytest.raises(ValueError, match='used|claimed|bound'):
        await spawn_owned(*argv, start_new_session=True, launch_record=record)
    assert len(owned_children) == 1


@pytest.mark.asyncio
async def test_invalid_launch_record_does_not_spawn(owned_children):
    from genesis.util.proc_kill import spawn_owned

    with pytest.raises(TypeError, match='LaunchRecord'):
        await spawn_owned(sys.executable, '-I', '-S', '-u', '-c', 'pass',
                          start_new_session=True, launch_record=object())
    assert not owned_children


@pytest.mark.asyncio
async def test_already_admitted_unit_cannot_acquire_after_quarantine():
    scope = owner()
    admitted, proceed = asyncio.Event(), asyncio.Event()

    class BadClient:
        async def aclose(self):
            raise OSError('controlled close refusal')

    client = BadClient()
    async with scope.unit():
        scope.retain_client(client)

    async def later():
        async with scope.unit():
            admitted.set()
            await proceed.wait()
            with pytest.raises(RuntimeError, match='unconfirmed'):
                scope.require_unit()

    task = asyncio.create_task(later())
    await admitted.wait()
    try:
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await scope.retire(client)
        proceed.set()
        await task
    finally:
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await scope.close()


@pytest.mark.asyncio
async def test_queued_stdio_sender_cannot_reuse_uncertain_generation(tmp_path, monkeypatch, owned_children):
    import genesis.mcp.local_modules as owners
    from genesis.util.proc_kill import ProcessCleanupUncertain

    actual_confirm = owners.confirm_process_exit

    async def refuse_confirmation(*args, **kwargs):
        raise ProcessCleanupUncertain('controlled group refusal')

    scope = owner()
    adapter = stdio(tmp_path, scope)
    first = asyncio.create_task(exchange(scope, adapter, 'held'))
    admitted = asyncio.Event()

    async def later():
        async with scope.unit():
            admitted.set()
            return await adapter.send('second')

    second = None
    try:
        await ready(tmp_path/'ready')
        second = asyncio.create_task(later())
        await admitted.wait()
        monkeypatch.setattr(owners, 'confirm_process_exit', refuse_confirmation)
        first.cancel()
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await first
        (tmp_path/'release').touch()
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await asyncio.wait_for(second, 5)
    finally:
        monkeypatch.setattr(owners, 'confirm_process_exit', actual_confirm)
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
async def test_idle_resource_gets_cleanup_attempt_while_another_unit_drains():
    import anyio

    scope = owner(cleanup_timeout_s=.05)
    entered, release = asyncio.Event(), asyncio.Event()

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    idle = Client()
    async with scope.unit():
        scope.retain_client(idle)

    async def pending():
        async with scope.unit():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                with anyio.CancelScope(shield=True):
                    await release.wait()

    task = asyncio.create_task(pending())
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await scope.close()
        assert idle.closed, 'idle sibling got no cleanup attempt during the shared phase'
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
async def test_active_http_borrower_blocks_its_close_but_not_idle_sibling():
    import anyio

    scope = owner(cleanup_timeout_s=.05)
    entered, release = asyncio.Event(), asyncio.Event()

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    active, idle = Client(), Client()
    async with scope.unit():
        scope.retain_client(active)
        scope.retain_client(idle)

    async def pending():
        async with scope.unit():
            with scope.use(active):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    with anyio.CancelScope(shield=True):
                        await release.wait()

    task = asyncio.create_task(pending())
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await scope.close()
        assert idle.closed and not active.closed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        await scope.close()
    assert active.closed


@pytest.fixture
def logical_processes(monkeypatch):
    """Lifetime ordering oracle only: fake identities can NEVER reach signals."""
    import genesis.mcp.local_modules as owners

    processes, retired, budgets = [], [], []

    def no_signal(*args, **kwargs):
        raise AssertionError('A logical identity must never reach a signal syscall')

    class Process:
        pid = 1
        returncode = 0

        async def communicate(self, **kwargs):
            return b'{"type":"result","result":"owned"}\n', b''

        def kill(self):
            no_signal()

    async def create(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        process = Process()
        processes.append(process)
        return process

    async def confirm(process, *, confirm_timeout_s, terminate=True):
        assert process in processes
        retired.append(process)
        budgets.append(confirm_timeout_s)

    monkeypatch.setattr(os, 'killpg', no_signal)
    monkeypatch.setattr(os, 'kill', no_signal)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr(owners, 'confirm_process_exit', confirm)
    return processes, retired, budgets


@pytest.mark.asyncio
async def test_successful_ssh_health_is_retired_before_next_operation(logical_processes):
    processes, retired, _ = logical_processes
    scope = owner()
    adapter = create_ipc_adapter(IPCConfig(method='ssh', ssh_host='owned.invalid'), owner=scope)
    try:
        async with scope.unit():
            assert await adapter.health_check('version', 200)
            assert retired == [processes[0]], 'completed readiness handle survived into the next operation'
            assert (await adapter.send('', {'prompt': 'owned'}, 'CC'))['text'] == 'owned'
            assert retired == processes
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_healthy_retirement_does_not_start_a_later_unit_cancel_budget(monkeypatch, logical_processes):
    _, _, budgets = logical_processes
    scope = owner()
    loop = asyncio.get_running_loop()
    actual_time = loop.time
    advance = 0
    monkeypatch.setattr(loop, 'time', lambda: actual_time() + advance)
    try:
        async with scope.unit():
            first = await scope.spawn('owned-logical-program')
            await scope.retire(first)
            advance = 40  # legitimate intervening work, not a production timer
            second = await scope.spawn('owned-logical-program')
            await scope.retire(second)
            assert budgets[-1] > 29, 'healthy readiness started the later cleanup deadline'
    finally:
        monkeypatch.setattr(loop, 'time', actual_time)
        await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('history', ['before', 'inside'])
async def test_handled_cancellation_does_not_time_healthy_retirement(monkeypatch, logical_processes, history):
    _, _, budgets = logical_processes
    scope = owner()
    loop = asyncio.get_running_loop()
    actual_time, advance = loop.time, 0
    monkeypatch.setattr(loop, 'time', lambda: actual_time() + advance)

    async def handle_cancel():
        asyncio.current_task().cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(0)

    async def operation():
        nonlocal advance
        if history == 'before':
            await handle_cancel()
        async with scope.unit():
            if history == 'inside':
                await handle_cancel()
            first = await scope.spawn('owned-logical-program')
            await scope.retire(first)
            advance = 40
            second = await scope.spawn('owned-logical-program')
            await scope.retire(second)
            assert budgets[-1] > 29, 'handled cancellation timed later healthy work'

    try:
        await asyncio.create_task(operation())
    finally:
        monkeypatch.setattr(loop, 'time', actual_time)
        await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['native', 'raised'])
@pytest.mark.parametrize('later', ['handled', 'nested-error'])
async def test_started_cancellation_cleanup_keeps_one_budget(monkeypatch, logical_processes, kind, later):
    _, _, budgets = logical_processes
    scope = owner()
    loop = asyncio.get_running_loop()
    actual_time, advance = loop.time, 0
    monkeypatch.setattr(loop, 'time', lambda: actual_time() + advance)

    async def second_retirement(second):
        nonlocal advance
        advance = 20
        await scope.retire(second)
        assert 0 < budgets[-1] < 11, 'started cancellation cleanup received a fresh allowance'

    async def operation():
        async with scope.unit():
            first = await scope.spawn('owned-logical-program')
            second = await scope.spawn('owned-logical-program')
            try:
                if kind == 'native':
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                raise asyncio.CancelledError
            except asyncio.CancelledError:
                await scope.retire(first)
                if later == 'nested-error':
                    try:
                        raise ValueError('controlled cleanup diagnostic')
                    except ValueError:
                        await second_retirement(second)
            if later == 'handled':
                await second_retirement(second)

    try:
        await asyncio.create_task(operation())
    finally:
        monkeypatch.setattr(loop, 'time', actual_time)
        await scope.close()


@pytest.mark.asyncio
async def test_cancellation_during_retirement_keeps_its_existing_budget(monkeypatch, logical_processes):
    import genesis.mcp.local_modules as owners

    processes, _, budgets = logical_processes
    scope = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    actual_time, advance = loop.time, 0
    actual_confirm = owners.confirm_process_exit
    monkeypatch.setattr(loop, 'time', lambda: actual_time() + advance)

    async def confirm(process, **kwargs):
        if process is processes[0]:
            entered.set()
            await release.wait()
        await actual_confirm(process, **kwargs)

    monkeypatch.setattr(owners, 'confirm_process_exit', confirm)

    async def operation():
        nonlocal advance
        async with scope.unit():
            first = await scope.spawn('owned-logical-program')
            second = await scope.spawn('owned-logical-program')
            with pytest.raises(asyncio.CancelledError):
                await scope.retire(first)
            advance = 20
            await scope.retire(second)
            assert 0 < budgets[-1] < 11, 'late cancellation reset the retirement allowance'

    task = asyncio.create_task(operation())
    try:
        await entered.wait()
        task.cancel()
        release.set()
        await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(loop, 'time', actual_time)
        await scope.close()


@pytest.mark.asyncio
async def test_interrupted_unknown_retirement_does_not_renew_final_cleanup(monkeypatch, logical_processes):
    import genesis.mcp.local_modules as owners

    processes, _, budgets = logical_processes
    scope = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    actual_time, advance = loop.time, 0
    actual_confirm = owners.confirm_process_exit
    monkeypatch.setattr(loop, 'time', lambda: actual_time() + advance)

    async def confirm(process, **kwargs):
        nonlocal advance
        if process is processes[0]:
            entered.set()
            await release.wait()
            advance = 20
            raise RuntimeError('controlled unconfirmed group')
        await actual_confirm(process, **kwargs)

    monkeypatch.setattr(owners, 'confirm_process_exit', confirm)

    async def operation():
        async with scope.unit():
            first = await scope.spawn('owned-logical-program')
            await scope.spawn('owned-logical-program')
            await scope.retire(first)

    task = asyncio.create_task(operation())
    try:
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(owners.ModuleOwnershipError, match='unconfirmed'):
            await task
        assert 0 < budgets[-1] < 11, 'unknown cleanup renewed the later finalization allowance'
        with pytest.raises(owners.ModuleOwnershipError, match='unconfirmed'):
            async with scope.unit():
                pytest.fail('unconfirmed resource admitted new work')
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(loop, 'time', actual_time)
        with pytest.raises(owners.ModuleOwnershipError, match='unconfirmed'):
            await scope.close()


@pytest.mark.asyncio
async def test_spontaneous_stdio_exit_is_retired_without_another_request(logical_processes):
    processes, retired, _ = logical_processes
    scope = owner()
    try:
        async with scope.unit():
            proc = await scope.spawn('owned-logical-program', persistent=True)
        # The public leader status can finish while pipe EOF/Process.wait does not.
        async def paused_pipes():
            await asyncio.Event().wait()
        proc.wait = paused_pipes
        async with asyncio.timeout(.5):
            while not retired:
                await asyncio.sleep(.01)
        assert retired == processes
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_expired_resource_retry_checks_absence_without_resignalling(monkeypatch, logical_processes):
    import genesis.mcp.local_modules as owners

    processes, _, _ = logical_processes
    scope = owner(cleanup_timeout_s=.01)
    calls = []

    async def confirm(proc, *, confirm_timeout_s, terminate=True):
        assert proc in processes
        calls.append(terminate)
        if terminate:
            raise RuntimeError('controlled confirmation refusal')

    monkeypatch.setattr(owners, 'confirm_process_exit', confirm)
    try:
        async with scope.unit():
            proc = await scope.spawn('owned-logical-program', persistent=True)
            proc.returncode = None
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await scope.retire(proc)
        await asyncio.sleep(.02)
        await scope.close()
        assert calls == [True, False], 'expired identity received another termination attempt'
    finally:
        # Logical oracle only; no syscall can escape the fixture's signal fence.
        async def confirmed(*args, **kwargs):
            pass
        monkeypatch.setattr(owners, 'confirm_process_exit', confirmed)
        await scope.close()


@pytest.mark.asyncio
async def test_explicit_http_retirement_refuses_new_borrowers():
    scope = owner()
    entered, release = asyncio.Event(), asyncio.Event()

    class Client:
        async def aclose(self):
            entered.set()
            await release.wait()

    client = Client()
    async with scope.unit():
        scope.retain_client(client)
    retiring = asyncio.create_task(scope.retire(client))
    try:
        await entered.wait()
        async with scope.unit():
            with pytest.raises(RuntimeError, match='retir'):
                with scope.use(client):
                    pytest.fail('new borrower used a retiring client')
    finally:
        release.set()
        await retiring
        await scope.close()


@pytest.mark.asyncio
async def test_exit_observer_failure_quarantines_then_drains(monkeypatch, logical_processes):
    processes, _, _ = logical_processes
    scope = owner()
    try:
        async with scope.unit():
            proc = await scope.spawn('owned-logical-program', persistent=True)
            proc.returncode = None
        def refused_status(self):
            raise OSError('controlled public status failure')
        monkeypatch.setattr(type(proc), 'returncode', property(refused_status))
        async with asyncio.timeout(2):
            while scope._observers:
                await asyncio.sleep(.01)
        with pytest.raises(RuntimeError, match='unconfirmed'):
            async with scope.unit():
                pytest.fail('failed observation admitted more work')
        assert processes == [proc]
    finally:
        await scope.close()
    assert scope.closed and not scope._observers


@pytest.mark.asyncio
async def test_exit_observer_explicit_retirement_and_close_share_one_cleanup(monkeypatch, logical_processes):
    import genesis.mcp.local_modules as owners

    processes, _, _ = logical_processes
    scope = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def confirm(proc, **kwargs):
        calls.append(proc)
        entered.set()
        await release.wait()

    monkeypatch.setattr(owners, 'confirm_process_exit', confirm)
    retiring = closing = None
    try:
        async with scope.unit():
            proc = await scope.spawn('owned-logical-program', persistent=True)
            proc.returncode = None
        retiring = asyncio.create_task(scope.retire(proc))
        await entered.wait()
        proc.returncode = 0
        closing = asyncio.create_task(scope.close())
        await asyncio.sleep(.15)  # allow the leader observer to join retirement
        release.set()
        await asyncio.gather(retiring, closing)
        assert calls == processes
        assert scope.closed and not scope._observers
    finally:
        release.set()
        await asyncio.gather(*(t for t in (retiring, closing) if t is not None), return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
async def test_actual_exited_stdio_leader_is_settled_with_paused_pipe(tmp_path, owned_children):
    scope = owner()
    proc = waiting = None
    script = 'import sys;sys.stdout.buffer.write(b"x"*16384);sys.stdout.flush()'
    try:
        async with scope.unit():
            proc = await scope.spawn(sys.executable, '-I', '-S', '-u', '-c', script,
                                     stdout=asyncio.subprocess.PIPE, limit=1024, persistent=True)
            waiting = asyncio.create_task(proc.wait())
        async with asyncio.timeout(5):
            while proc.returncode is None or scope._resources:
                await asyncio.sleep(.01)
        assert proc.returncode == 0
        assert not waiting.done(), 'pipe did not exercise wait-after-leader-exit distinction'
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
        assert await proc.stdout.read() == b'x'*16384
        await waiting
    finally:
        if proc is not None:
            await proc.stdout.read()
        if waiting is not None:
            await waiting
        await scope.close()
    assert not scope._observers


@pytest.mark.parametrize('state', ['alive', 'denied', 'absent-unreaped', 'absent-reaped'])
@pytest.mark.asyncio
async def test_expired_confirmation_never_signals_and_requires_full_absence(monkeypatch, state):
    from types import SimpleNamespace

    from genesis.util.proc_kill import ProcessCleanupUncertain, confirm_process_exit

    probes = []
    def probe(pid, sig):
        assert pid == 1729 and sig == 0, 'expired identity received a termination signal'
        probes.append((pid, sig))
        if state.startswith('absent'):
            raise ProcessLookupError
        if state == 'denied':
            raise PermissionError

    def no_signal(*args, **kwargs):
        raise AssertionError('logical process cannot reach any actual signal syscall')

    monkeypatch.setattr(os, 'killpg', probe)
    monkeypatch.setattr(os, 'kill', no_signal)
    proc = SimpleNamespace(pid=1729, returncode=None if state == 'absent-unreaped' else 0, kill=no_signal)
    if state == 'absent-reaped':
        await confirm_process_exit(proc, terminate=False, confirm_timeout_s=0)
    else:
        with pytest.raises(ProcessCleanupUncertain):
            await confirm_process_exit(proc, terminate=False, confirm_timeout_s=0)
    assert probes == [(1729, 0)]
