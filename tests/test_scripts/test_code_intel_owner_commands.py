"""Captured owner commands keep one exec child and a request-only monitor."""
import os
import runpy
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_owned_child import toy as toy
from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def prepared(tmp_path, monkeypatch):
    _, _, snapshot = case(tmp_path, monkeypatch, 'runner')
    modules = snapshot.assemble()
    value = runtime(snapshot, modules)
    snapshot = snapshot.with_runtime(value)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_owner.py'))
    return snapshot, modules, value, namespace


def test_scope_command_is_one_literal_exec_with_closed_full_policy(tmp_path, monkeypatch):
    snapshot, modules, value, owner = prepared(tmp_path, monkeypatch)
    command = owner['scope_command'](snapshot, modules, 7)
    assert command[:5] == ['/bin/bash', '--noprofile', '--norc', '-p', '-c']
    tokens = shlex.split(command[5])
    assert tokens[:5] == ['exec', '/usr/bin/systemd-run', '--user', '--scope', '--quiet']
    for token in ('--no-ask-password', '--expand-environment=no', '--slice=genesis-workload.slice',
                  '--unit=' + value['reservation']['unit'], 'MemoryMax=8589934592',
                  'MemorySwapMax=0', 'IOWeight=50', 'CPUQuota=200%'):
        assert token in tokens
    assert tokens[tokens.index('--') + 1:] == snapshot.resume_command('reporter', '7')
    assert value['environment_recipe']['frozen']['SYNTHETIC_RUNTIME_VALUE'] not in repr(command)
    assert not any(token in tokens for token in ('eval', 'source', '|', '&', '-s'))


@pytest.mark.parametrize('descriptor', [0, 1, 2, True, -1, '7'])
def test_scope_command_rejects_nonprivate_ack_descriptor(tmp_path, monkeypatch, descriptor):
    snapshot, modules, _, owner = prepared(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        owner['scope_command'](snapshot, modules, descriptor)


def test_monitor_uses_held_buffers_after_paths_disappear(tmp_path, monkeypatch, toy):
    snapshot, modules, value, owner = prepared(tmp_path, monkeypatch)
    value['policy']['watchdog'].update(interval='.02', warmup_interval='.02', continuous_pause_max='1')
    snapshot = snapshot.with_runtime(value)
    for name in ('scripts/lib/proc_pressure.sh', 'scripts/lib/code_intel_scope.sh'):
        (snapshot.root / name).unlink()
    command = owner['monitor_command'](snapshot, modules, toy.pid)
    env = {**os.environ, 'CODE_INTEL_FAKE_LOADAVG': '100', 'CODE_INTEL_FAKE_IOWAIT': '100',
           'BASH_ENV': '/a/startup/file/that/does/not/exist'}
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, text=True, start_new_session=True)
    try:
        assert [child.stdout.readline().strip() for _ in range(3)] == ['ready', 'freeze', 'pressure_stop']
        assert child.poll() is None, 'completed policy waits for owner retirement'
        child.stdin.close()
        child.stdin = None
        output, errors = child.communicate(timeout=10)
        assert child.returncode == 0 and output == '', errors
        assert 'pausing index' in errors
    finally:
        if child.stdin is not None:
            child.stdin.close()
            child.stdin = None
        child.communicate(timeout=10)
    assert toy.poll() is None, 'monitor must never signal its observed child'
    assert value['environment_recipe']['frozen']['SYNTHETIC_RUNTIME_VALUE'] not in repr(command)


def test_monitor_channel_failure_is_fatal_before_policy_loop(tmp_path, monkeypatch, toy):
    snapshot, modules, _, owner = prepared(tmp_path, monkeypatch)
    command = owner['monitor_command'](snapshot, modules, toy.pid)
    # A read-only descriptor makes the first ready write fail synchronously.
    reader, writer = os.pipe()
    try:
        with open('/dev/null', 'rb') as output:
            result = subprocess.run(command, stdin=reader, stdout=output,
                                    stderr=subprocess.PIPE, timeout=10, start_new_session=True)
    finally:
        os.close(reader)
        os.close(writer)
    assert result.returncode != 0 and toy.poll() is None


def test_owned_monitor_eof_retirement_finishes_successfully(tmp_path, monkeypatch, toy):
    snapshot, modules, value, owner = prepared(tmp_path, monkeypatch)
    value['policy']['watchdog'].update(interval='.02', warmup_interval='.02')
    command = owner['monitor_command'](snapshot.with_runtime(value), modules, toy.pid)
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True, env={**os.environ,
        'CODE_INTEL_FAKE_LOADAVG': '0', 'CODE_INTEL_FAKE_IOWAIT': '0'})
    try:
        assert child.stdout.readline() == b'ready\n'
        child.stdin.close()  # Only the original owner holds the private writer.
        child.stdin = None
        output, errors = child.communicate(timeout=10)
        assert child.returncode == 0, errors
        assert output == b'' and toy.poll() is None
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, 0)
    finally:
        if child.stdin is not None:
            child.stdin.close()
            child.stdin = None
        child.communicate(timeout=10)
