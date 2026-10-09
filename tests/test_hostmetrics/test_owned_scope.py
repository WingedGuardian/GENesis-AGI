"""Actual named-scope cooperative cleanup, including a changed-group descendant."""
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time

import pytest

from genesis.hostmetrics import jobs, run


def test_native_scope_waits_for_second_cancel_after_leader_exit(tmp_path):
    if not shutil.which('systemd-run'):
        pytest.skip('systemd-run unavailable')
    probe = subprocess.run(['systemctl', '--user', 'show-environment'], env=jobs.systemd_env(), capture_output=True, timeout=5)
    if probe.returncode:
        pytest.skip('systemd user manager unavailable')
    unit = run.unit_name('cooperative-native-test', str(os.getpid()))
    ready = tmp_path / 'descendant.pid'
    descendant = (
        'import os,signal,time; os.setpgrp(); signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        f'from pathlib import Path; p=Path({str(ready)!r}); '
        'p.with_suffix(".staged").write_text(str(os.getpid())); '
        'os.replace(p.with_suffix(".staged"),p); time.sleep(90)'
    )
    worker = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{descendant!r}]); time.sleep(90)'
    argv = run.scope_argv(unit, run.scope_properties(128 * 2**20, 10), None, sys.executable, '-c', worker)
    with run.OwnedProcess() as owner:
        owner.start_scoped(argv, unit, env=jobs.systemd_env(), start_new_session=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                assert time.monotonic() < deadline, 'descendant did not start'
                assert not owner.exited(), 'scope launcher exited before readiness'
                time.sleep(.05)
            descendant_pid = int(ready.read_text())
            os.kill(os.getpid(), signal.SIGTERM)
            owner.checkpoint()
            # Deliberately exceed the rejected 15-second automatic force policy.
            until = time.monotonic() + 16
            while time.monotonic() < until:
                owner.checkpoint()
                time.sleep(.05)
            os.kill(descendant_pid, 0)
            assert run._scope_quiescent(unit) is False
            os.kill(os.getpid(), signal.SIGINT)
            owner.wait(timeout=15)
            assert run._scope_quiescent(unit) is True
        finally:
            if not owner.reaped:
                owner.stop(signal.SIGKILL)
                owner.wait(timeout=15)
    assert owner.outcome(0) == 130


def test_native_registration_refusal_does_not_stop_existing_scope(tmp_path):
    probe = subprocess.run(['systemctl', '--user', 'show-environment'],
                           env=jobs.systemd_env(), capture_output=True, timeout=5)
    if probe.returncode:
        pytest.skip('systemd user manager unavailable')
    unit = run.unit_name('collision-native-test', secrets.token_hex(16))
    ready = tmp_path / 'existing.pid'
    payload = (f'import os,time; from pathlib import Path; p=Path({str(ready)!r}); '
               'p.with_suffix(".staged").write_text(str(os.getpid())); '
               'os.replace(p.with_suffix(".staged"),p); time.sleep(90)')
    argv = run.scope_argv(unit, run.scope_properties(64 * 2**20, 10), None,
                         sys.executable, '-c', payload)
    existing = subprocess.Popen(argv, env=jobs.systemd_env(), start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert time.monotonic() < deadline
            assert existing.poll() is None
            time.sleep(.05)
        old_pid = int(ready.read_text())
        with run.OwnedProcess() as contender:
            contender.start_scoped(argv, unit, env=jobs.systemd_env(), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert contender.wait(timeout=15) != 0
            assert contender._scope_authorized is False
            contender._forward(signal.SIGINT, None)
            contender.checkpoint()
        os.kill(old_pid, 0)
        assert existing.poll() is None
        assert run._scope_quiescent(unit) is False
        assert contender.outcome(0) == 130
    finally:
        # This unit belongs to this test's original launch, never the contender.
        subprocess.run(['systemctl', '--user', 'kill', '--signal=KILL', unit+'.scope'],
                       env=jobs.systemd_env(), capture_output=True, timeout=15)
        existing.wait(timeout=15)


def test_native_gate_and_probe_fit_existing_minimum_ram(capsys):
    probe = subprocess.run(['systemctl', '--user', 'show-environment'],
                           env=jobs.systemd_env(), capture_output=True, timeout=5)
    if probe.returncode:
        pytest.skip('systemd user manager unavailable')
    for _ in range(3):
        readback = (
            'cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); '
            'test "$(cat "$cg/memory.max")" = 16777216 && '
            'test "$(cat "$cg/memory.swap.max")" = 0 && '
            'test "$(cat "$cg/cpu.max")" = "10000 100000"'
        )
        assert run.launch('minimum-gate-'+secrets.token_hex(4), ['/bin/sh', '-c', readback],
                          run.MIN_RAM, 10, lambda: None) == 0
    assert 'killed at its memory cap' not in capsys.readouterr().err
