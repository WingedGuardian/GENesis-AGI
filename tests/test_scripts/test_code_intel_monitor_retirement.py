"""Real held Bash monitor, private EOF retirement and harmless toy workload."""
import os
import select
import signal
import subprocess

import pytest

from tests.test_scripts.test_code_intel_owned_child import toy as toy
from tests.test_scripts.test_code_intel_owner_commands import prepared
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def retire(run):
    if run['writer'] is not None:
        os.close(run['writer'])
        run['writer'] = None


@pytest.fixture
def monitors(tmp_path, monkeypatch, toy):
    snapshot, modules, value, owner = prepared(tmp_path, monkeypatch)
    runs = []

    def start(*, interval='.02', payload=None, phase=None):
        value['policy']['watchdog'].update(interval=interval, warmup_interval=interval)
        command = owner['monitor_command'](snapshot.with_runtime(value), modules, toy.pid)
        hooks = {
            'wall': "_wall_cap() { printf 'phase\\n' >&2; command sleep .03; printf 14400; }\n",
            'load': "pressure_loadavg1() { printf 'phase\\n' >&2; command sleep .03; printf 0; }\n",
            'iowait': "pressure_iowait_pct() { printf 'phase\\n' >&2; command sleep .03; printf 0; }\n",
            'sleep': "sleep() { printf 'phase\\n' >&2; command sleep \"$@\"; }\n",
        }
        if phase:
            assert command[5].count('_wd_send ready\n') == 1
            command[5] = command[5].replace('_wd_send ready\n', hooks[phase] + '_wd_send ready\n')
        reader, writer = os.pipe()
        if payload:
            os.write(writer, payload)
        if payload == b'':
            os.close(writer)
            writer = None
        try:
            child = subprocess.Popen(command, stdin=reader, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
                env={**os.environ, 'CODE_INTEL_FAKE_LOADAVG': '0', 'CODE_INTEL_FAKE_IOWAIT': '0'})
        finally:
            os.close(reader)
        run = dict(child=child, writer=writer, cleanup_timeout=max(1, float(interval) + 1))
        runs.append(run)
        return run

    yield start
    for run in runs:
        retire(run)
        child = run['child']
        try:
            child.communicate(timeout=run['cleanup_timeout'])
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)  # Only this fixture's disposable session.
            child.communicate(timeout=5)
    assert toy.poll() is None


def ready(run):
    child = run['child']
    assert select.select([child.stdout], [], [], 5)[0]
    assert child.stdout.readline() == b'ready\n'


def completed(run, *, timeout=3):
    output, errors = run['child'].communicate(timeout=timeout)
    assert run['child'].returncode == 0 and output == b'' and errors == b''
    with pytest.raises(ProcessLookupError):
        os.killpg(run['child'].pid, 0)


@pytest.mark.parametrize('phase', [None] * 20 + ['wall', 'load', 'iowait', 'sleep'])
def test_eof_at_ready_substitution_and_sleep_phases(monitors, phase):
    run = monitors(phase=phase)
    ready(run)
    if phase:
        assert select.select([run['child'].stderr], [], [], 5)[0]
        assert run['child'].stderr.readline() == b'phase\n'
    retire(run)
    completed(run)


def test_default_interval_drains_under_its_existing_policy(monitors):
    run = monitors(interval='15', phase='sleep')
    ready(run)
    assert run['child'].stderr.readline() == b'phase\n'
    retire(run)
    completed(run, timeout=20)  # The measured foreground sleep can take15s.


def test_open_pipe_and_leaked_writer_are_not_retirement(monitors):
    run = monitors()
    ready(run)
    duplicate = os.dup(run['writer'])
    try:
        retire(run)
        assert not select.select([run['child'].stdout], [], [], .1)[0]
        assert run['child'].poll() is None
    finally:
        os.close(duplicate)
    completed(run)


def test_eof_before_readiness_suppresses_the_ready_record(monitors):
    run = monitors(payload=b'')
    completed(run)


@pytest.mark.parametrize('payload', [b'\0', b'\n', b'x', b'\xc3', b'\xff'])
@pytest.mark.parametrize('early', [True, False])
def test_every_invalid_byte_class_is_fatal(monitors, payload, early):
    run = monitors(payload=payload if early else None)
    if not early:
        ready(run)
        os.write(run['writer'], payload)
    output, errors = run['child'].communicate(timeout=3)
    assert run['child'].returncode == 125 and output == b'' and errors == b''


def test_external_term_remains_monitor_failure(monitors):
    run = monitors()
    ready(run)
    os.kill(run['child'].pid, signal.SIGTERM)
    run['child'].communicate(timeout=3)
    assert run['child'].returncode == -signal.SIGTERM
