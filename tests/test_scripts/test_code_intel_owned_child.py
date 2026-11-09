"""Actual toy children: raw wait ownership is cached, never inferred from Popen."""
import runpy
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def toy():
    child = subprocess.Popen([sys.executable, '-I', '-c',
                              'import sys;print("ready",flush=True);sys.stdin.buffer.read(1)'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert child.stdout.readline() == b'ready\n'
    yield child
    if child.poll() is None:
        child.kill()
    child.wait()
    child.stdin.close()
    child.stdout.close()


def wrap(child, evidence):
    path = ROOT / 'scripts/lib/code_intel_owner.py'
    assert path.is_file(), 'finite physical owner must retain actual child wait ownership'
    namespace = runpy.run_path(str(path))
    return namespace['OwnedChild'](child, SimpleNamespace(**evidence))


def test_actual_wait_is_cached_once_and_identity_names_owned_child(toy, evidence, monkeypatch):
    owned = wrap(toy, evidence)
    assert owned.status() is None and owned.identity['pid'] == toy.pid
    toy.stdin.close()
    observed = toy.stdout.read()
    assert observed == b''
    # An actual blocking raw wait is only a fixture oracle; feed its actual
    # terminal status through the same decoder rather than Popen.wait/poll.
    import os
    waited = os.waitpid(toy.pid, 0)
    calls = []
    original = os.waitpid
    def once(pid, flags):
        calls.append((pid, flags))
        return waited
    monkeypatch.setattr(os, 'waitpid', once)
    assert owned.status() == 0
    assert owned.status() == 0 and len(calls) == 1
    monkeypatch.setattr(os, 'waitpid', original)


def test_external_reap_is_sticky_unavailable_even_when_popen_has_success(toy, evidence):
    owned = wrap(toy, evidence)
    toy.stdin.close()
    assert toy.wait() == 0
    with pytest.raises(ChildProcessError):
        owned.status()
    assert toy.returncode == 0
    with pytest.raises(ChildProcessError):
        owned.status()


def test_only_actual_owned_unreaped_birth_can_receive_a_signal(toy, evidence):
    owned = wrap(toy, evidence)
    owned.terminate()
    toy.stdout.read()
    import os
    pid, status = os.waitpid(toy.pid, 0)
    assert pid == toy.pid and os.waitstatus_to_exitcode(status) == -signal.SIGTERM
    toy.returncode = -signal.SIGTERM  # fixture owns the oracle reap, not owner proof


def test_changed_birth_never_signals_a_matching_pid(toy, evidence, monkeypatch):
    owned = wrap(toy, evidence)
    namespace = owned.terminate.__globals__
    actual = namespace['os'].kill
    calls = []
    monkeypatch.setattr(namespace['os'], 'kill', lambda *args: calls.append(args))
    process = owned.proof._process_state
    def changed(*args):
        pid, start, parent = process(*args)
        return pid, start + 1, parent
    monkeypatch.setattr(owned.proof, '_process_state', changed)
    with pytest.raises(ValueError):
        owned.terminate()
    assert not calls
    monkeypatch.setattr(namespace['os'], 'kill', actual)


def test_foreign_process_is_not_owned_even_with_a_popen_shaped_value(evidence):
    with pytest.raises((ValueError, TypeError)):
        wrap(SimpleNamespace(pid=__import__('os').getpid(), returncode=None), evidence)
