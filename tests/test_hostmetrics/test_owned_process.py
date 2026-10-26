"""Real Linux child ownership and caller-cancellation contracts."""
import errno
import os
import select
import signal
import subprocess
import sys
import threading
import time
import types

import pytest

from genesis.hostmetrics import run


def _child(code=0):
    return subprocess.Popen([sys.executable, '-c', f'raise SystemExit({code})'], start_new_session=True)


@pytest.mark.parametrize('code', [0, 7])
def test_real_child_status(code):
    with run.OwnedProcess() as owner:
        child = _child(code)
        owner.bind(child)
        assert owner.wait(timeout=5) == code
    assert owner.outcome(code) == code
    assert child.returncode == code


def test_numeric_dispatch_closed_before_consuming_wait(monkeypatch):
    real_waitpid = os.waitpid
    sent = []
    monkeypatch.setattr(run, 'kill_group', lambda *args: sent.append(args))
    with run.OwnedProcess() as owner:
        child = _child()
        owner.bind(child)

        def consume(pid, flags):
            result = real_waitpid(pid, flags)
            owner._forward(signal.SIGINT, None)
            owner.stop(signal.SIGKILL)
            return result

        monkeypatch.setattr(run.os, 'waitpid', consume)
        assert owner.wait(timeout=5) == 0
    assert sent == []
    assert owner.outcome(0) == 130


def test_watchdog_only_enqueues_and_does_not_count_as_caller(monkeypatch):
    sent = []
    monkeypatch.setattr(run, 'kill_group', lambda *args: sent.append(args))
    with run.OwnedProcess() as owner:
        child = _child()
        owner.bind(child)
        producer = threading.Thread(target=owner.stop)
        producer.start()
        producer.join()
        assert sent == []
        assert owner.signals == 0
        owner.checkpoint()
        assert sent == [(child.pid, signal.SIGTERM)]
        owner.wait(timeout=5)
    assert owner.outcome(0) == 0


def test_second_cancellation_forces_without_deadline(monkeypatch):
    sent = []
    monkeypatch.setattr(run, 'kill_group', lambda *args: sent.append(args))
    with run.OwnedProcess() as owner:
        child = _child()
        owner.bind(child)
        owner._forward(signal.SIGHUP, None)
        owner.checkpoint()
        owner.checkpoint()
        assert sent == [(child.pid, signal.SIGTERM)]
        owner._forward(signal.SIGINT, None)
        owner.checkpoint()
        assert sent == [(child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)]
        owner.wait(timeout=5)
    assert owner.outcome(0) == 130


def test_first_cancellation_after_reap_remains_cooperative_for_owned_scope(monkeypatch):
    sent = []
    monkeypatch.setattr(run, 'kill_group', lambda *_args: pytest.fail('reaped PID cannot be signalled'))
    monkeypatch.setattr(run, '_signal_scope', lambda unit, sig: sent.append((unit, sig)) or True)
    monkeypatch.setattr(run, '_scope_quiescent', lambda _unit: bool(sent and sent[-1][1] == signal.SIGKILL))
    with run.OwnedProcess() as owner:
        child = _child(7)
        owner.bind(child, 'synthetic-owned-scope')
        # Model an acknowledged scope retaining children after the real leader exits.
        owner._scope_authorized = True
        assert owner.wait(timeout=5) == 7
        assert owner.reaped and not owner._numeric_open
        owner._forward(signal.SIGTERM, None)
        for _ in range(3):
            owner.checkpoint()
        assert sent == [('synthetic-owned-scope', signal.SIGTERM)]
        assert owner._scope_authorized and not owner._finished
        assert child.returncode == 7
        owner._forward(signal.SIGINT, None)
        owner.checkpoint()
        assert sent == [('synthetic-owned-scope', signal.SIGTERM),
                        ('synthetic-owned-scope', signal.SIGKILL)]
        assert owner.wait(timeout=5) == 7
        assert not owner._scope_authorized
    assert owner.outcome(7) == 130


def test_external_reap_is_not_fabricated_success():
    child = None
    with pytest.raises(run.ProbeRefused, match='consumed externally'), run.OwnedProcess() as owner:
        child = _child(7)
        owner.bind(child)
        child.wait(timeout=5)  # unsupported competing consumer, deliberately exercised
        owner.wait(timeout=5)
    assert child.returncode == 7
    assert owner._numeric_open is False


def test_nondefault_sigchld_refused_before_spawn():
    previous = signal.getsignal(signal.SIGCHLD)
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        with pytest.raises(run.ProbeRefused, match='default SIGCHLD'), run.OwnedProcess():
            pytest.fail('must refuse before entering launch body')
    finally:
        signal.signal(signal.SIGCHLD, previous)


def test_requested_name_without_registration_never_signals_or_queries_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(run, '_signal_scope', lambda *args: calls.append(('signal', args)))
    monkeypatch.setattr(run, '_scope_quiescent', lambda *args: calls.append(('query', args)))
    with run.OwnedProcess() as owner:
        owner.bind(_child(), 'unrelated-existing.scope')
        owner.stop(signal.SIGKILL)
        owner.wait(timeout=5)
    assert calls == []


def test_buffered_registration_replays_strongest_stop_and_retries_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(run, '_signal_scope', lambda unit, sig: calls.append(sig) or len(calls) > 1)
    monkeypatch.setattr(run, '_scope_quiescent', lambda unit: True)
    with run.OwnedProcess() as owner:
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        owner._adopt(_child(), 'owned.scope', reader)
        owner.stop(signal.SIGKILL)
        owner.checkpoint()
        assert calls == []
        os.write(writer, b'1')
        os.close(writer)
        owner.checkpoint()
        assert calls == [signal.SIGKILL]
        owner.stop(signal.SIGTERM)  # Never downgrade an already requested kill.
        owner.checkpoint()
        assert calls == [signal.SIGKILL, signal.SIGKILL]
        owner.wait(timeout=5)
    assert owner._registration_fd == -1


@pytest.mark.parametrize('fault', ['invalid', 'duplicate', 'read'])
@pytest.mark.parametrize('stage', ['body', 'finalization'])
def test_registration_fault_still_reaps_child_without_scope_authority(monkeypatch, fault, stage):
    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    os.write(writer, b'11' if fault == 'duplicate' else b'2')
    os.close(writer)
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                             start_new_session=True)
    original_read = os.read
    if fault == 'read':
        def broken(fd, amount):
            if fd == reader:
                raise OSError(errno.EIO, 'injected registration read fault')
            return original_read(fd, amount)
        monkeypatch.setattr(run.os, 'read', broken)
    owner = run.OwnedProcess()
    named_calls = []
    monkeypatch.setattr(run, '_signal_scope', lambda *args: named_calls.append(args))
    try:
        with pytest.raises(run.ProbeRefused), owner:
            owner._adopt(child, 'unregistered.scope', reader)
            if stage == 'body':
                owner.checkpoint()
        assert owner.reaped is True
        assert child.returncode is not None
        assert named_calls == []
    finally:
        monkeypatch.setattr(run.os, 'read', original_read)
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


@pytest.mark.parametrize('fault', ['invalid', 'duplicate', 'read', 'close'])
def test_terminal_registration_fault_restores_handlers_and_surfaces_error(monkeypatch, fault):
    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    child = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'raise SystemExit(7)'],
                             start_new_session=True)
    owner = run.OwnedProcess()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    actual_os, actual_signal = run.os, run.signal
    proxy_os = types.SimpleNamespace(**{key: getattr(os, key) for key in dir(os)})
    proxy_signal = types.SimpleNamespace(**{key: getattr(signal, key) for key in dir(signal)})
    terminal = False
    def masked(how, values):
        nonlocal terminal
        result = actual_signal.pthread_sigmask(how, values)
        if how == signal.SIG_BLOCK and not terminal:
            terminal = True
            if fault != 'close':
                owner._forward(signal.SIGINT, None)
        return result
    def read(fd, count):
        if fd != reader:
            return actual_os.read(fd, count)
        if not terminal:
            raise BlockingIOError(errno.EAGAIN, 'injected pending proof')
        if fault == 'read':
            raise OSError(errno.EIO, 'injected terminal proof fault')
        return b'11' if fault == 'duplicate' else b'2'
    def close(fd):
        actual_os.close(fd)
        if terminal and fault == 'close' and fd == reader:
            raise OSError(errno.EIO, 'injected terminal close fault')
    proxy_os.read, proxy_os.close = read, close
    proxy_signal.pthread_sigmask = masked
    monkeypatch.setattr(run, 'os', proxy_os)
    monkeypatch.setattr(run, 'signal', proxy_signal)
    monkeypatch.setattr(run, '_signal_scope', lambda *args: pytest.fail('no registration authority'))
    try:
        with pytest.raises((run.ProbeRefused, OSError)), owner:
            owner._adopt(child, 'unregistered.scope', reader)
            assert owner.wait(timeout=5) == 7
        assert owner.reaped and owner._finished and not owner._numeric_open
        assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())
        assert owner._registration_error is not None
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        os.close(writer)


def test_unknown_scope_cleanup_does_not_discard_leader_wait_status(monkeypatch):
    monkeypatch.setattr(run, '_signal_scope', lambda *args: True)
    monkeypatch.setattr(run, '_scope_quiescent', lambda *args: None)
    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    os.write(writer, b'1')
    os.close(writer)
    child = _child(7)
    owner = run.OwnedProcess()
    try:
        with pytest.raises(run.ProbeRefused), owner:
            owner._adopt(child, 'registered.scope', reader)
            deadline = time.monotonic() + 5
            while not owner.exited():
                assert time.monotonic() < deadline
                time.sleep(.01)
            owner.stop(signal.SIGTERM)
            owner.wait(timeout=5)
        assert owner.reaped is True
        assert child.returncode == 7
    finally:
        if child.returncode is None:
            child.wait(timeout=5)


@pytest.mark.parametrize('fault', ['invalid', 'duplicate', 'read'])
@pytest.mark.parametrize('first_failure', [2, 3, 4])
@pytest.mark.parametrize('caller', [False, True])
def test_late_finalization_registration_fault_retains_reap(monkeypatch, fault, first_failure, caller):
    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    os.write(writer, b'11' if fault == 'duplicate' else b'2')
    os.close(writer)
    child = subprocess.Popen([
        sys.executable, '-I', '-S', '-c',
        'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        'print("ready",flush=True);time.sleep(60)',
    ], start_new_session=True, stdout=subprocess.PIPE)
    ready = select.poll()
    ready.register(child.stdout.fileno(), select.POLLIN)
    assert ready.poll(3000) and child.stdout.readline() == b'ready\n'
    original_read = os.read
    reads = 0
    def delayed(fd, amount):
        nonlocal reads
        if fd == reader:
            reads += 1
            if reads < first_failure:
                raise BlockingIOError(errno.EAGAIN, 'registration pending')
            if fault == 'read':
                raise OSError(errno.EIO, 'injected late registration fault')
        return original_read(fd, amount)
    monkeypatch.setattr(run.os, 'read', delayed)
    owner = run.OwnedProcess()
    named_calls = []
    signals = []
    original_kill = run.kill_group
    def kill(pid, sig):
        signals.append((sig, owner._numeric_open))
        original_kill(pid, sig)
    monkeypatch.setattr(run, 'kill_group', kill)
    cooperative = []
    def second_cancel():
        cooperative.append(not owner._finished and not owner.reaped
                           and owner._numeric_sent == signal.SIGTERM)
        owner._forward(signal.SIGINT, None)
    timer = threading.Timer(.4, second_cancel)
    monkeypatch.setattr(run, '_signal_scope', lambda *args: named_calls.append(args))
    try:
        with pytest.raises(run.ProbeRefused), owner:
            owner._adopt(child, 'unregistered.scope', reader)
            if caller:
                owner._forward(signal.SIGINT, None)
                timer.start()
        assert owner.reaped
        assert child.returncode is not None
        assert not owner._numeric_open
        assert named_calls == []
        assert signals == ([(signal.SIGTERM, True), (signal.SIGKILL, True)]
                           if caller else [(signal.SIGKILL, True)])
        if caller:
            assert cooperative == [True]
            assert owner.outcome(0) == 130
    finally:
        timer.cancel()
        if caller:
            timer.join()
        child.stdout.close()
        monkeypatch.setattr(run.os, 'read', original_read)
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
