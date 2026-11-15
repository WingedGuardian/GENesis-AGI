"""Isolated subreaper: actual owned IPC, creation faults and controlled trees."""

import asyncio
import ctypes
import json
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
directory, method, ending = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
home = directory/'home'
home.mkdir()
os.environ.clear()
os.environ.update(HOME=str(home), GENESIS_HOME=str(home/'.genesis'), GENESIS_MCP_ENV_RECORDER='0')

TREE = """
import json,os,sys,select,signal
from pathlib import Path
guardian=int(sys.argv[3])
assert not select.select([guardian],[],[],0)[0]
child=os.fork()
if child==0:
    select.select([guardian],[],[])
    os._exit(0)
target=Path(sys.argv[1]);pending=target.with_suffix('.pending')
try:
    pending.write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgid(0),'child':child}));pending.replace(target)
except BaseException:
    os.kill(child,signal.SIGKILL);os.waitpid(child,0)
    raise
if sys.argv[2]!='normal':
    select.select([guardian],[],[])
    os.waitpid(child,0)
    os._exit(0)
os.kill(child,signal.SIGKILL);os.waitpid(child,0)
print(json.dumps({'type':'result','result':'owned'}),flush=True)
"""
real_popen = subprocess.Popen
observed = []


class OwnedOnly(real_popen):
    def __init__(self, argv, *args, **kwargs):
        assert list(argv[:6]) == [sys.executable, '-I', '-S', '-u', '-c', TREE]
        assert kwargs['start_new_session'] is True
        super().__init__(argv, *args, **kwargs)
        observed.append(self)


def refuse(*args, **kwargs):
    raise AssertionError('Process controller denies network')


subprocess.Popen = OwnedOnly
socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = refuse
libc = ctypes.CDLL(None, use_errno=True)
assert sys.platform == 'linux' and libc.prctl(36, 1, 0, 0, 0) == 0
subreaper = ctypes.c_int()
assert libc.prctl(37, ctypes.byref(subreaper), 0, 0, 0) == 0 and subreaper.value == 1
guardian_fd = os.pidfd_open(os.getpid())
assert not select.select([guardian_fd], [], [], 0)[0]

from genesis.mcp.local_modules import ModuleResourceOwner  # noqa: E402
from genesis.modules.external.config import IPCConfig  # noqa: E402
from genesis.modules.external.ipc import create_ipc_adapter  # noqa: E402


async def main():
    scope = ModuleResourceOwner(grace_s=0, cleanup_timeout_s=.2 if ending == 'pipe-failure' else 30)
    actual_create = asyncio.create_subprocess_exec
    loop = asyncio.get_running_loop()
    original_pipe = loop.connect_read_pipe
    gate = asyncio.Event()
    ready = directory/'ready'
    descendants, reaped = set(), set()
    outcome = {'method': method, 'ending': ending, 'observer_rescue': False}

    async def substitute(*argv, **kwargs):
        assert argv[0] == 'ssh'
        return await actual_create(sys.executable, '-I', '-S', '-u', '-c', TREE,
                                   str(ready), ending, str(guardian_fd), pass_fds=(guardian_fd,), **kwargs)

    async def pipe(*args, **kwargs):
        await gate.wait()
        raise OSError('owned injected pipe setup failure')

    async def reap():
        while True:
            for pid in descendants-reaped:
                try:
                    got, _ = os.waitpid(pid, os.WNOHANG)
                    if got == pid:
                        reaped.add(pid)
                except ChildProcessError:
                    pass  # not adopted yet; never turn this into proof of death
            await asyncio.sleep(.01)

    async def invoke():
        async with scope.unit():
            return await adapter.send('owned-command', {'prompt': 'owned'}, method)

    asyncio.create_subprocess_exec = substitute
    if ending == 'pipe-failure':
        loop.connect_read_pipe = pipe
    adapter = create_ipc_adapter(IPCConfig(method='ssh', ssh_host='owned.invalid'), owner=scope)
    reaper = asyncio.create_task(reap())
    task = asyncio.create_task(invoke())
    info = None
    try:
        while not ready.exists():
            assert not task.done(), 'subject failed before controlled readiness'
            await asyncio.sleep(.01)
        info = json.loads(ready.read_text())
        assert info['pid'] == info['pgid'] == observed[0].pid
        descendants.add(info['child'])
        if ending == 'pipe-failure':
            gate.set()
            scope.begin_draining()
            try:
                await scope.close()
            except RuntimeError as exc:
                assert 'unconfirmed' in str(exc)
                outcome['unconfirmed'] = True
            else:
                raise AssertionError('faulted unreturned launch was certified closed')
            try:
                async with scope.unit():
                    raise AssertionError('unknown launch admitted work')
            except RuntimeError:
                outcome['new_admission_refused'] = True
            assert not scope.closed and scope._resources
        else:
            if ending == 'cancel':
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    outcome['cancelled'] = True
                else:
                    raise AssertionError('cancellation did not propagate')
            else:
                assert 'owned' in json.dumps(await task)
            await scope.close()
            assert scope.closed and not scope._resources
            assert ending == 'normal' or info['child'] in reaped
            try:
                os.killpg(info['pid'], 0)
            except ProcessLookupError:
                outcome['group_absence_confirmed'] = True
            else:
                raise AssertionError('owned group survived reported success')
    finally:
        gate.set()
        loop.connect_read_pipe = original_pipe
        # Rescue is explicitly outside production. Signal only our adopted,
        # waitpid-confirmed child, or the direct Popen handle we created.
        if info is not None and info['child'] not in reaped:
            try:
                got, _ = os.waitpid(info['child'], os.WNOHANG)
                if got == 0:
                    os.kill(info['child'], 9)
                    outcome['observer_rescue'] = True
                else:
                    reaped.add(info['child'])
            except ChildProcessError:
                pass
        for proc in observed:
            if proc.returncode is None:
                os.killpg(proc.pid, 9)
                outcome['observer_rescue'] = True
        for resource in scope._resources:
            if resource.record and resource.record.task and not resource.record.task.done():
                resource.record.task.cancel()
            if resource.launch and not resource.launch.done():
                resource.launch.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if info is not None:
            while ending != 'normal' and info['child'] not in reaped:
                await asyncio.sleep(.01)
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        asyncio.create_subprocess_exec = actual_create
        (directory/'result.json').write_text(json.dumps(outcome, indent=2)+'\n')
    if ending != 'pipe-failure':
        assert not outcome['observer_rescue']


async def bounded():
    async with asyncio.timeout(30):
        await main()


asyncio.run(bounded())
