"""Private ACK framing on real anonymous pipes; no scope or provider launch."""

import errno
import os
import runpy
import threading
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_index_marker_execution import _claim, _registration, _reservation
from tests.test_scripts.test_index_marker_legs import queue as queue


@pytest.fixture
def handshake(queue, tmp_path):
    context = _claim(queue, tmp_path)["context"]
    claimed = {"context": context, "mode": "fast"}
    reserved = _reservation(claimed)
    scope = _registration(claimed)
    value = dict(version=1, reservation_sha256=queue._native.digest(reserved),
                 owner=deepcopy(reserved["owner"]), foreground=deepcopy(scope["facts"]["reporter"]),
                 unit_path="/org/freedesktop/systemd1/unit/synthetic_2escope", scope=scope)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/lib/code_intel_queued_worker.py"))
    return queue, context, reserved, value, namespace


def _gate(handshake):
    queue, context, reserved, value, namespace = handshake
    factory = namespace.get("StartupGate")
    assert callable(factory), "reporter needs the actual closed startup ACK gate"
    return factory(queue, queue._proof, context, reserved)


def _receive(gate, raw):
    reader, writer = os.pipe()
    failures = []

    def produce():
        try:
            offset = 0
            while offset < len(raw):
                offset += os.write(writer, raw[offset:])
        except BrokenPipeError:
            pass  # Expected if the receiver refuses oversized output early.
        except Exception as error:
            failures.append(error)
        finally:
            os.close(writer)

    child = threading.Thread(target=produce)
    child.start()
    try:
        return gate.receive(reader)
    finally:
        child.join(2)
        assert not child.is_alive() and not failures


def test_closed_ack_roundtrip_consumes_both_descriptors(handshake):
    gate = _gate(handshake)
    value = handshake[3]
    reader, writer = os.pipe()
    gate.send(writer, value)
    with pytest.raises(OSError):
        os.fstat(writer)
    assert gate.receive(reader) == value
    with pytest.raises(OSError):
        os.fstat(reader)


@pytest.mark.parametrize("counter", ["events", "local_events"])
def test_original_sampled_kill_refuses_before_ack_release(handshake, counter):
    gate = _gate(handshake)
    value = deepcopy(handshake[3])
    value["scope"]["facts"][counter]["oom_kill"] = 1
    with pytest.raises(ValueError, match="owned OOM"):
        gate.validate(value)


def test_reporter_waits_for_complete_record_and_eof(handshake):
    gate = _gate(handshake)
    reader, writer = os.pipe()
    entered, completed = threading.Event(), threading.Event()
    results = []

    def reporter():
        entered.set()
        try:
            results.append(gate.receive(reader))
        finally:
            completed.set()

    child = threading.Thread(target=reporter)
    child.start()
    assert entered.wait(2)
    raw = handshake[0]._native.encode(handshake[3]) + b"\n"
    os.write(writer, raw)
    assert not completed.wait(0.05), "record bytes alone released the reporter before EOF"
    os.close(writer)
    child.join(2)
    assert not child.is_alive() and results == [handshake[3]]


@pytest.mark.parametrize("fault", ["empty", "partial", "duplicate", "second", "oversize", "missing-newline", "extra-line"])
def test_incomplete_or_ambiguous_wire_refuses(handshake, fault):
    gate = _gate(handshake)
    raw = handshake[0]._native.encode(handshake[3])
    value = {"empty": b"", "partial": raw[:-1],
             "duplicate": raw[:-1] + b',"version":1}\n',
             "second": raw + b"\n{}\n", "oversize": b"x" * 4097,
             "missing-newline": raw, "extra-line": raw + b"\n\n"}[fault]
    with pytest.raises(ValueError):
        _receive(gate, value)


@pytest.mark.parametrize("fault", ["version", "version-bool", "missing", "extra", "reservation", "owner",
                                  "owner-bool", "foreground", "foreground-bool", "unit-path", "unit",
                                  "scope-shape", "invocation", "budget", "boot", "cap", "owned-oom"])
def test_ack_binds_the_closed_original_reservation_and_child(handshake, fault):
    gate = _gate(handshake)
    value = deepcopy(handshake[3])
    if fault == "version":
        value["version"] = 2
    elif fault == "version-bool":
        value["version"] = True
    elif fault == "missing":
        del value["scope"]
    elif fault == "extra":
        value["extra"] = 1
    elif fault == "reservation":
        value["reservation_sha256"] = "a" * 64
    elif fault == "owner":
        value["owner"]["start_ticks"] += 1
    elif fault == "owner-bool":
        value["owner"]["pid"] = True
    elif fault == "foreground":
        value["foreground"]["pid"] += 1
    elif fault == "foreground-bool":
        value["foreground"]["start_ticks"] = True
    elif fault == "unit-path":
        value["unit_path"] = "/org/freedesktop/systemd1/foreign"
    elif fault == "unit":
        value["scope"]["unit"] += "-foreign"
    elif fault == "scope-shape":
        value["scope"]["extra"] = 1
    elif fault == "invocation":
        value["scope"]["invocation_id"] = "bad"
    elif fault == "budget":
        value["scope"]["stop_usec"] = 2**63
    elif fault == "boot":
        value["scope"]["facts"]["reporter"]["boot_id"] = "00000000-0000-0000-0000-000000000001"
    elif fault == "cap":
        value["scope"]["facts"]["memory_max"] //= 2
    elif fault == "owned-oom":
        value["scope"]["facts"]["events"]["oom_kill"] = 1
    with pytest.raises(ValueError):
        _receive(gate, handshake[0]._native.encode(value) + b"\n")


def test_foreign_regular_or_wrong_direction_fd_is_preserved(handshake, tmp_path):
    gate = _gate(handshake)
    path = tmp_path / "unchanged"
    path.write_text("original")
    with path.open("rb") as stream:
        with pytest.raises(ValueError):
            gate.receive(stream.fileno())
        assert stream.read() == b"original"
    reader, writer = os.pipe()
    try:
        with pytest.raises(ValueError):
            gate.receive(writer)
        with pytest.raises(ValueError):
            gate.send(reader, handshake[3])
        os.fstat(reader)
        os.fstat(writer)
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize("fd", [True, False, 0, 1, 2, -1, "3"])
def test_invalid_descriptor_never_consumes_stdio(handshake, fd):
    gate = _gate(handshake)
    with pytest.raises(ValueError):
        gate.receive(fd)
    for standard in (0, 1, 2):
        os.fstat(standard)


@pytest.mark.parametrize("failure", ["broken", "full", "short"])
def test_failed_nonblocking_release_never_reports_success(handshake, monkeypatch, failure):
    gate = _gate(handshake)
    reader, writer = os.pipe()
    original = os.write
    try:
        if failure == "broken":
            os.close(reader)
            reader = None
        elif failure == "full":
            os.set_blocking(writer, False)
            with pytest.raises(BlockingIOError):
                while True:
                    os.write(writer, b"x" * 4096)
        else:
            monkeypatch.setattr(os, "write", lambda fd, raw: original(fd, raw[:-1]) if fd == writer else original(fd, raw))
        with pytest.raises(OSError) as error:
            gate.send(writer, handshake[3])
        assert error.value.errno in (errno.EPIPE, errno.EAGAIN, errno.EIO)
        with pytest.raises(OSError):
            os.fstat(writer)
    finally:
        if reader is not None:
            os.close(reader)


@pytest.mark.parametrize("method", ["send", "receive"])
@pytest.mark.parametrize("failure", ["capacity", "cloexec"])
def test_valid_pipe_is_closed_when_setup_fails(handshake, monkeypatch, method, failure):
    gate = _gate(handshake)
    reader, writer = os.pipe()
    target = writer if method == "send" else reader
    if failure == "capacity":
        monkeypatch.setattr(os, "fpathconf", lambda *args: -1)
    else:
        def refuse(*args):
            raise OSError(errno.EIO, "synthetic descriptor setup failure")
        monkeypatch.setattr(os, "set_inheritable", refuse)
    try:
        with pytest.raises((OSError, ValueError)):
            if method == "send":
                gate.send(target, handshake[3])
            else:
                gate.receive(target)
        with pytest.raises(OSError):
            os.fstat(target)
    finally:
        for fd in (reader, writer):
            with suppress(OSError):
                os.close(fd)


def test_named_fifo_is_refused_without_consuming_its_descriptor(handshake, tmp_path):
    gate = _gate(handshake)
    path = tmp_path / "foreign-fifo"
    os.mkfifo(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(ValueError):
            gate.receive(fd)
        os.fstat(fd)
    finally:
        os.close(fd)


def test_preflight_counts_the_entire_actual_ack_before_any_write(handshake):
    gate = _gate(handshake)
    value = deepcopy(handshake[3])
    value["scope"]["facts"]["ancestors"][0]["events"]["synthetic_" + "x" * 4096] = 0
    reader, writer = os.pipe()
    try:
        with pytest.raises(ValueError, match="capacity"):
            gate.send(writer, value)
        assert os.read(reader, 1) == b""
    finally:
        os.close(reader)
