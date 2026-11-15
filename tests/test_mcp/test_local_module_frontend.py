"""Actual ordinary CLI, shared TCP floor and real local module child bodies."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import importlib.util
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from tests.test_mcp.test_shared_cli_startup_contract import shared_install as shared_install
from tests.test_mcp.test_shared_forwarder import (
    LEGACY,
    MODERN,
    ROOT,
    _finish_health_backend,
    _health_backend,
    _health_frame,
    _health_frontend_environment,
    receive,
    send,
)

MODULE = """
import json,os,sys,time
from pathlib import Path
ready,release,receipt=map(Path,sys.argv[1:])
for line in sys.stdin:
    request=json.loads(line)
    row={'path':request['path'],'pid':os.getpid(),'pgid':os.getpgid(0),
         'birth':Path('/proc/self/stat').read_text().rpartition(')')[2].split()[19]}
    with receipt.open('a') as f:f.write(json.dumps(row)+'\\n')
    if request['path']=='held':
        pending=ready.with_suffix('.pending');pending.write_text(json.dumps(row));pending.replace(ready)
        while not release.exists():time.sleep(.01)
        ready.with_suffix('.completed').touch()
    print(json.dumps(row),flush=True)
"""


async def observed(path):
    async with asyncio.timeout(120):
        while not path.exists():
            await asyncio.sleep(.02)
    return json.loads(path.read_text())


async def reply(child, request_id):
    async with asyncio.timeout(120):
        while True:
            response = await receive(child, timeout=120)
            if response.get('id') == request_id and type(response.get('id')) is type(request_id):
                return response


def group_gone(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def lifecycle_event(path, event):
    async with asyncio.timeout(120):
        while True:
            if path.exists():
                rows = [json.loads(line) for line in path.read_text().splitlines(keepends=True) if line.endswith('\n')]
                if any(row['event'] == event for row in rows):
                    return
            await asyncio.sleep(.02)


@pytest.mark.parametrize('service', ['health'])
@pytest.mark.parametrize('era,ending,no_db', [
    (MODERN, 'cancel', False), (MODERN, 'EOF', False),
    (MODERN, 'cancel', True), (LEGACY, 'EOF', True),
    (MODERN, 'grace', False), (LEGACY, 'grace', True),
], ids=['db-cancel-modern', 'db-eof-modern', 'no-db-cancel-modern', 'no-db-eof-legacy',
        'db-grace-modern', 'no-db-grace-legacy'])
async def test_actual_module_body_cancel_or_eof_settles_before_dependencies(
    tmp_path, service, era, ending, no_db, shared_install,
):
    injection = tmp_path/'injection'
    injection.mkdir()
    (injection/'sitecustomize.py').write_text(
        'import os,traceback\ntry:\n'
        ' from tests.test_mcp.local_module_fixture import install\n install()\n'
        'except BaseException:\n traceback.print_exc()\n os._exit(72)\n')
    remote = _health_frontend_environment(shared_install, tmp_path/'backend', injection, role='backend', mixed=False)
    local = _health_frontend_environment(shared_install, tmp_path/'mixed', injection, role='mixed', mixed=True)
    ready, release, receipts = (tmp_path/name for name in ('module-ready', 'module-release', 'module-receipts'))
    command = [sys.executable, '-I', '-S', '-u', '-c', MODULE, str(ready), str(release), str(receipts)]
    local['LOCAL_MODULE_TEST_COMMAND'] = json.dumps(command)
    if ending == 'grace':
        config_path = Path(local['GENESIS_HOME'])/'config/mcp_shared.json'
        config = json.loads(config_path.read_text())
        config['servers']['health']['shutdown_grace_s'] = 2
        config_path.write_text(json.dumps(config))
    if no_db:
        local['GENESIS_DB_PATH'] = str(tmp_path/'absent.db')
    modules = Path(local['GENESIS_HOME'])/'config/modules'
    modules.mkdir()
    # JSON is a YAML subset; no test parser substitutes for the production loader.
    (modules/'owned.yaml').write_text(json.dumps({
        'type': 'external', 'name': 'owned-fixture', 'enabled': True,
        'ipc': {'method': 'stdio', 'command': command},
        'health_check': {'endpoint': 'health'},
        'operations': {'held': {'path': 'held'}, 'next': {'path': 'next'}},
    }))
    backend = frontend = None
    births = {}
    with (tmp_path/'backend.stderr').open('w') as backend_errors, (tmp_path/'frontend.stderr').open('w') as errors:
        try:
            backend = await _health_backend(shared_install, backend_errors, remote)
            frontend = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT/'scripts/genesis_mcp_server.py'), '--server', 'health',
                cwd='/', env=local, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errors)
            if era == LEGACY:
                await send(frontend, _health_frame(era, 'init', 'initialize', protocolVersion=era,
                    capabilities={}, clientInfo={'name': 'fixture', 'version': '1'}))
                assert 'result' in await reply(frontend, 'init')
                await send(frontend, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            await send(frontend, _health_frame(era, 0, 'tools/call', name='module_call',
                arguments={'module_name': 'owned-fixture', 'operation': 'held'}))
            first = await observed(ready)
            births[first['pid']] = first
            assert first['pgid'] == first['pid'] and not group_gone(first['pid'])
            if ending == 'grace':
                frontend.stdin.close()
                await lifecycle_event(Path(local['LOCAL_HEALTH_TEST_REPORT']), 'module-owner-draining')
                assert not group_gone(first['pid']), 'EOF bypassed the configured completion grace'
                release.touch()
            if ending == 'cancel':
                await send(frontend, {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 0}})
                # A separate remote call proves the one shared backend still works.
                await send(frontend, _health_frame(era, 'remote', 'tools/call', name='health_status', arguments={}))
                remote_reply = await reply(frontend, 'remote')
                assert 'result' in remote_reply and not remote_reply['result'].get('isError')
                async with asyncio.timeout(10):
                    while not group_gone(first['pid']):
                        await asyncio.sleep(.02)
                release.touch()
                await send(frontend, _health_frame(era, 'next', 'tools/call', name='module_call',
                    arguments={'module_name': 'owned-fixture', 'operation': 'next'}))
                response = await reply(frontend, 'next')
                assert 'result' in response and not response['result'].get('isError'), response
                body = response['result'].get('structuredContent')
                assert body['path'] == 'next' and body['pid'] != first['pid']
                births[body['pid']] = body
            frontend.stdin.close()
            assert await asyncio.wait_for(frontend.wait(), 40) == 0
            if ending == 'grace':
                assert ready.with_suffix('.completed').exists(), 'accepted work did not finish within grace'
            rows = [json.loads(line) for line in receipts.read_text().splitlines()]
            assert sum(row['path'] == 'held' for row in rows) == 1
            assert all(group_gone(row['pid']) for row in rows)
            lifecycle = [json.loads(line) for line in Path(local['LOCAL_HEALTH_TEST_REPORT']).read_text().splitlines()]
            closed = next(i for i, row in enumerate(lifecycle) if row['event'] == 'module-owner-closed')
            db_closes = [i for i, row in enumerate(lifecycle) if row['event'] == 'db-close-start']
            assert no_db or (db_closes and closed < min(db_closes))
        finally:
            if frontend is not None and frontend.returncode is None:
                frontend.terminate()
                await asyncio.wait_for(frontend.wait(), 10)
            # Failure-only rescue: signal only an exact birth of our synthetic
            # process group. Passing assertions above require natural settlement.
            for row in births.values():
                path = Path(f'/proc/{row["pid"]}/stat')
                with suppress(FileNotFoundError, ProcessLookupError):
                    birth = path.read_text().rpartition(')')[2].split()[19]
                    if birth == row['birth'] and os.getpgid(row['pid']) == row['pid']:
                        os.killpg(row['pid'], 9)
            await _finish_health_backend(backend, shared_install)


@pytest.mark.parametrize('service', ['health'])
@pytest.mark.parametrize('repeat', [False, True], ids=['once', 'repeat'])
async def test_confirmed_drain_cancellation_finishes_dependency_closers(monkeypatch, shared_install, repeat):
    from fastmcp import FastMCP

    from genesis.db import connection
    from genesis.mcp import health
    from genesis.mcp.health import browser
    from genesis.mcp.local_modules import ModuleResourceOwner

    spec = importlib.util.spec_from_file_location('owned_lifespan_fixture', ROOT/'scripts/genesis_mcp_server.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, '_DEFAULT_DB', Path(shared_install.env['GENESIS_DB_PATH']))
    monkeypatch.setattr(launcher, '_DEFAULT_STATUS', Path(shared_install.env['GENESIS_HOME'])/'status.json')
    monkeypatch.setattr(launcher, '_init_standalone_health_router', lambda: None)

    async def no_actions(db):
        return None

    monkeypatch.setattr(launcher, '_init_standalone_health_actions', no_actions)
    monkeypatch.setattr(health, 'mcp', FastMCP('owned-lifespan-fixture'))
    for name in ('_service', '_event_bus', '_activity_tracker', '_job_retry_registry'):
        monkeypatch.setattr(health, name, getattr(health, name))
    events, connections = [], []
    client_entered, client_release = asyncio.Event(), asyncio.Event()
    browser_entered, browser_release = asyncio.Event(), asyncio.Event()
    if not repeat:
        browser_release.set()
    original_get_db, original_close = connection.get_db, connection.SerializedConnection.close

    async def opened(*args, **kwargs):
        db = await original_get_db(*args, **kwargs)
        connections.append(db)
        return db

    async def db_close(db):
        events.append('db')
        await original_close(db)

    async def browser_close():
        events.append('browser')
        browser_entered.set()
        await browser_release.wait()
        events.append('browser-done')

    monkeypatch.setattr(connection, 'get_db', opened)
    monkeypatch.setattr(connection.SerializedConnection, 'close', db_close)
    monkeypatch.setattr(browser, 'async_cleanup', browser_close)
    scope = ModuleResourceOwner(grace_s=0)

    class Client:
        async def aclose(self):
            client_entered.set()
            await client_release.wait()

    async with scope.unit():
        scope.retain_client(Client())
    server = launcher._prepare_health(module_owner=scope)

    async def lifecycle():
        async with server._lifespan(server):
            pass

    task = asyncio.create_task(lifecycle())
    try:
        await asyncio.wait_for(client_entered.wait(), 10)
        task.cancel()
        client_release.set()
        if repeat:
            await asyncio.wait_for(browser_entered.wait(), 10)
            task.cancel()
            browser_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert scope.closed
        assert events == ['browser', 'browser-done', 'db'], 'confirmed cleanup was skipped on cancellation'
    finally:
        client_release.set()
        browser_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for db in connections:
            await original_close(db)  # failure-only/duplicate-safe real DB rescue
        await scope.close()


@pytest.mark.parametrize('service', ['health'])
@pytest.mark.parametrize('no_db', [False, True], ids=['db', 'no-db'])
async def test_actual_eof_unknown_launch_does_not_close_dependencies(tmp_path, service, no_db, shared_install):
    injection = tmp_path/'injection'
    injection.mkdir()
    (injection/'sitecustomize.py').write_text(
        'import os,traceback\ntry:\n'
        ' from tests.test_mcp.local_module_fixture import install\n install()\n'
        'except BaseException:\n traceback.print_exc()\n os._exit(72)\n')
    remote = _health_frontend_environment(shared_install, tmp_path/'backend', injection, role='backend', mixed=False)
    local = _health_frontend_environment(shared_install, tmp_path/'mixed', injection, role='mixed', mixed=True)
    marker = tmp_path/'module-started'
    script = ("import json,os,sys,threading;from pathlib import Path;"
              "p=Path(sys.argv[1]);q=p.with_suffix('.pending');"
              "q.write_text(json.dumps({'pid':os.getpid(),'birth':Path('/proc/self/stat').read_text().rpartition(')')[2].split()[19]}));"
              "q.replace(p);threading.Event().wait()")
    command = [sys.executable, '-I', '-S', '-u', '-c', script, str(marker)]
    local.update(LOCAL_MODULE_TEST_COMMAND=json.dumps(command), LOCAL_MODULE_TEST_PIPE_FAILURE=str(marker))
    if no_db:
        local['GENESIS_DB_PATH'] = str(tmp_path/'absent.db')
    modules = Path(local['GENESIS_HOME'])/'config/modules'
    modules.mkdir()
    (modules/'owned.yaml').write_text(json.dumps({
        'type': 'external', 'name': 'owned-fixture', 'enabled': True,
        'ipc': {'method': 'stdio', 'command': command},
        'health_check': {'endpoint': 'health'}, 'operations': {'held': {'path': 'held'}},
    }))
    backend = frontend = None
    with (tmp_path/'backend.stderr').open('w') as backend_errors, (tmp_path/'frontend.stderr').open('w') as errors:
        try:
            backend = await _health_backend(shared_install, backend_errors, remote)
            frontend = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT/'scripts/genesis_mcp_server.py'), '--server', 'health',
                cwd='/', env=local, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errors)
            await send(frontend, _health_frame(MODERN, 0, 'tools/call', name='module_call',
                arguments={'module_name': 'owned-fixture', 'operation': 'held'}))
            await observed(marker)
            frontend.stdin.close()
            assert await asyncio.wait_for(frontend.wait(), 45) != 0
            lifecycle = [json.loads(line) for line in Path(local['LOCAL_HEALTH_TEST_REPORT']).read_text().splitlines()]
            assert any(r['event'] == 'module-created' for r in lifecycle)
            assert any(r['event'] == 'injected-pipe-failure' for r in lifecycle)
            assert not any(r['event'] in {'module-owner-closed', 'db-close-start', 'browser-cleanup-returned'} for r in lifecycle)
            assert 'unconfirmed' in (tmp_path/'frontend.stderr').read_text()
        finally:
            if frontend is not None and frontend.returncode is None:
                frontend.terminate()
                await asyncio.wait_for(frontend.wait(), 10)
            report = Path(local['LOCAL_HEALTH_TEST_REPORT'])
            if report.exists():
                for row in map(json.loads, report.read_text().splitlines()):
                    if row['event'] == 'module-created':
                        with suppress(FileNotFoundError, ProcessLookupError):
                            birth = Path(f'/proc/{row["pid"]}/stat').read_text().rpartition(')')[2].split()[19]
                            if birth == row['birth'] and os.getpgid(row['pid']) == row['pid']:
                                os.killpg(row['pid'], 9)
            await _finish_health_backend(backend, shared_install)


@pytest.mark.parametrize('era', [MODERN, LEGACY], ids=['modern', 'legacy'])
@pytest.mark.parametrize('handled_history', [False, True], ids=['clean', 'handled'])
async def test_owner_stop_is_contained_to_the_module_operation(monkeypatch, era, handled_history):
    import socket
    import subprocess
    from contextvars import ContextVar

    from fastmcp import FastMCP
    from fastmcp.client.transports.base import TransportOptions
    from fastmcp.client.transports.memory import FastMCPTransport
    from fastmcp.server.dependencies import get_context
    from mcp import ClientSession
    from mcp.shared.exceptions import MCPError
    from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

    from genesis.mcp.local_modules import ModuleResourceOwner

    def refuse(*args, **kwargs):
        raise AssertionError('SDK unit-boundary test permits no process or network')

    monkeypatch.setattr(subprocess, 'Popen', refuse)
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)
    owner = ModuleResourceOwner(grace_s=0)
    server = FastMCP('module-unit-boundary')
    entered, finished = asyncio.Event(), asyncio.Event()
    outcome = {}
    marker = ContextVar('module-operation-context-proof')

    @server.tool()
    async def held() -> dict:
        token = marker.set(object())
        context, value = get_context(), marker.get()
        request_id = context.request_id

        async def operation():
            assert marker.get() is value and get_context() is context
            assert context.request_id == request_id and context.request_context is not None
            if handled_history:
                asyncio.current_task().cancel('handled cancellation inside the operation')
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0)
            entered.set()
            await asyncio.Event().wait()
        try:
            await owner.run(operation)
        finally:
            marker.reset(token)
        pytest.fail('stopped operation silently fell through as success')

    @server.tool()
    async def witness() -> dict:
        return {'alive': True}

    class RawSession(ClientSession):
        def __init__(self, read_stream, write_stream, **kwargs):
            self.relay = JSONRPCDispatcher(read_stream, write_stream)
            super().__init__(dispatcher=self.relay, **kwargs)

    async def invoke(session):
        frame = _health_frame(era, 0, 'tools/call', name='held', arguments={})
        try:
            outcome['reply'] = await session.relay.send_raw_request(frame['method'], frame['params'],
                {'request_id': frame['id'], 'cancel_on_abandon': True})
        except MCPError as error:
            outcome['wire_error'] = str(error)
        finally:
            finished.set()

    pending = None
    try:
        async with FastMCPTransport(server).connect_session(
            transport_options=TransportOptions(session_class=RawSession),
        ) as session:
            if era == LEGACY:
                frame = _health_frame(era, 'init', 'initialize', protocolVersion=era, capabilities={},
                                      clientInfo={'name': 'fixture', 'version': '1'})
                await session.relay.send_raw_request(frame['method'], frame['params'], {'request_id': frame['id']})
                await session.relay.notify('notifications/initialized', None)
            pending = asyncio.create_task(invoke(session))
            await asyncio.wait_for(entered.wait(), 10)
            await owner.close()
            await asyncio.wait_for(finished.wait(), 10)
            frame = _health_frame(era, 'after', 'tools/call', name='witness', arguments={})
            result = await session.relay.send_raw_request(frame['method'], frame['params'], {'request_id': frame['id']})
            assert result['structuredContent'] == {'alive': True}, 'operation stop killed the SDK connection'
            assert outcome.get('reply', {}).get('isError') is True, outcome
            assert owner.closed
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await owner.close()
