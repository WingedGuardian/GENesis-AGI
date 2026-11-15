"""Real process trees in isolated controllers; no provider/SSH/model program."""

import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def guardian(monkeypatch):
    """Outside the expendable controller: adopt/reap only this test's trees."""
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    children = Path(f'/proc/self/task/{os.getpid()}/children')
    assert not children.read_text().split(), 'guardian requires an isolated parent child population'
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    actual = subprocess.Popen
    controls = []
    controller = str(Path(__file__).with_name('local_module_process_child.py'))
    reaped, forced = [], []

    class ControllerOnly(actual):
        def __init__(self, argv, *args, **kwargs):
            assert list(argv[:3]) == [sys.executable, '-I', controller] and not controls
            super().__init__(argv, *args, **kwargs)
            controls.append(self)

    monkeypatch.setattr(subprocess, 'Popen', ControllerOnly)

    def settle():
        deadline = time.monotonic() + 5
        while descendants := children.read_text().split():
            for raw in descendants:
                pid = int(raw)
                got, status = os.waitpid(pid, os.WNOHANG)
                if got:
                    reaped.append((pid, os.waitstatus_to_exitcode(status)))
                elif time.monotonic() >= deadline:
                    # Only a waitpid-confirmed adopted child can be signalled.
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    forced.append(pid)
            time.sleep(.01)
        return reaped, forced

    try:
        yield settle
    finally:
        for proc in controls:
            if proc.returncode is None:
                proc.kill()
                proc.wait(timeout=5)
        settle()
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


@pytest.mark.parametrize('method', ['SHELL', 'CC'])
@pytest.mark.parametrize('ending', ['normal', 'cancel', 'pipe-failure'])
async def test_actual_creation_and_group_settlement(tmp_path, method, ending, guardian):
    controller = Path(__file__).with_name('local_module_process_child.py')
    with (tmp_path/'stderr').open('w') as errors:
        child = await asyncio.create_subprocess_exec(sys.executable, '-I', str(controller),
            str(tmp_path), method, ending, stdout=asyncio.subprocess.DEVNULL, stderr=errors)
        try:
            assert await asyncio.wait_for(child.wait(), 40) == 0, (tmp_path/'stderr').read_text()
            report = json.loads((tmp_path/'result.json').read_text())
            if ending == 'pipe-failure':
                assert report['unconfirmed'] and report['new_admission_refused']
            else:
                assert report['group_absence_confirmed'] and not report['observer_rescue']
        finally:
            if child.returncode is None:
                child.terminate()
                await child.wait()


async def test_controller_sigterm_still_settles_the_entire_tree(tmp_path, guardian):
    controller = Path(__file__).with_name('local_module_process_child.py')
    with (tmp_path/'stderr').open('w') as errors:
        child = await asyncio.create_subprocess_exec(sys.executable, '-I', str(controller),
            str(tmp_path), 'SHELL', 'guardian', stdout=asyncio.subprocess.DEVNULL, stderr=errors)
        try:
            async with asyncio.timeout(10):
                while not (tmp_path/'ready').exists():
                    assert child.returncode is None
                    await asyncio.sleep(.01)
            info = json.loads((tmp_path/'ready').read_text())
            assert info['pid'] == info['pgid']
            os.kill(info['pid'], 0)
            os.kill(info['child'], 0)
            child.terminate()  # default SIGTERM bypasses the controller's finally
            assert await asyncio.wait_for(child.wait(), 5) == -signal.SIGTERM
            reaped, forced = guardian()
            assert reaped and not forced
            with pytest.raises(ProcessLookupError):
                os.killpg(info['pid'], 0)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
