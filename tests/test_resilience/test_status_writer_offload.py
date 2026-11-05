"""Status publication stays ordered without blocking the asyncio loop."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.resilience.state import ResilienceStateMachine
from genesis.resilience.status_writer import StatusFileWriter


async def spin_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("operation", ["mkdir", "mkstemp", "replace"])
async def test_filesystem_stall_does_not_block_loop(monkeypatch, tmp_path, operation):
    path = tmp_path / "status.json"
    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), path=str(path))
    await writer.write()
    previous = path.read_bytes()
    entered, release = threading.Event(), threading.Event()
    threads = []

    def block():
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "test release did not arrive"

    if operation == "mkdir":
        original = Path.mkdir

        def mkdir(directory, *args, **kwargs):
            if directory == path.parent:
                block()
            return original(directory, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", mkdir)
    elif operation == "mkstemp":
        original = tempfile.mkstemp

        def mkstemp(*args, **kwargs):
            if kwargs.get("dir") == str(path.parent):
                block()
            return original(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    else:
        original = os.replace

        def replace(source, destination, *args, **kwargs):
            if Path(destination) == path:
                block()
            return original(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)

    # An independent thread releases even the unfixed, blocked event loop.
    # That turns the regression into an assertion failure instead of a hang.
    guard = threading.Timer(2, release.set)
    guard.start()
    writer.set_extra_data("sequence", 1)
    pending = asyncio.create_task(writer.write())
    try:
        await spin_until(entered.is_set)
        for _ in range(10):
            await asyncio.sleep(0)
        assert not release.is_set(), "filesystem call blocked the event loop"
        assert threads[0] != threading.get_ident()
        assert path.read_bytes() == previous
        writer.set_extra_data("sequence", 2)
    finally:
        release.set()
        guard.cancel()
        guard.join()
        await pending
    assert json.loads(path.read_text())["sequence"] == 1


@pytest.fixture
def held_publication(monkeypatch, tmp_path):
    writer = StatusFileWriter(
        state_machine=ResilienceStateMachine(), path=str(tmp_path / "status.json"),
    )
    entered, release = threading.Event(), threading.Event()
    payloads = []
    publish = StatusFileWriter._publish

    def held(path, payload):
        payloads.append(json.loads(payload))
        if len(payloads) == 1:
            entered.set()
            assert release.wait(5), "test release did not arrive"
        publish(path, payload)

    monkeypatch.setattr(StatusFileWriter, "_publish", staticmethod(held))
    yield writer, entered, release, payloads
    release.set()


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_publications_are_serial_after_caller_cancellation(held_publication, cancel_first):
    writer, entered, release, payloads = held_publication
    writer.set_extra_data("sequence", 1)
    first = asyncio.create_task(writer.write())
    await spin_until(entered.is_set)
    writer.set_extra_data("sequence", 2)
    second = asyncio.create_task(writer.write())
    canceled = asyncio.create_task(writer.write())
    await asyncio.sleep(0)
    canceled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceled
    try:
        if cancel_first:
            first.cancel()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(payloads) == 1
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    assert [p["sequence"] for p in payloads] == [1, 2]
    assert json.loads(writer._path.read_text())["sequence"] == 2


async def test_close_can_be_canceled_then_retried(held_publication):
    writer, entered, release, payloads = held_publication
    first = asyncio.create_task(writer.write())
    await spin_until(entered.is_set)
    queued = asyncio.create_task(writer.write())
    await asyncio.sleep(0)
    close = asyncio.create_task(writer.aclose())
    await asyncio.sleep(0)
    try:
        assert not close.done()
        with pytest.raises(RuntimeError, match="closed"):
            await writer.write()
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        close = asyncio.create_task(writer.aclose())
        await asyncio.sleep(0)
        assert not close.done()
    finally:
        release.set()
        await first
        await close
    with pytest.raises(RuntimeError, match="closed"):
        await queued
    await writer.aclose()
    assert len(payloads) == 1
    assert writer._publication is None


async def test_close_drains_collection_without_moving_db_work_off_loop(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    loop_thread = threading.get_ident()

    async def count():
        assert threading.get_ident() == loop_thread
        entered.set()
        await release.wait()
        return 7

    writer = StatusFileWriter(
        state_machine=ResilienceStateMachine(),
        deferred_queue=SimpleNamespace(count_pending=count), path=str(tmp_path / "status.json"),
    )
    pending = asyncio.create_task(writer.write())
    await entered.wait()
    close = asyncio.create_task(writer.aclose())
    await asyncio.sleep(0)
    try:
        assert not close.done()
    finally:
        release.set()
        await pending
        await close
    assert json.loads(writer._path.read_text())["queue_depths"]["deferred_work"] == 7


async def test_cancel_collection_releases_admission(tmp_path):
    entered = asyncio.Event()

    async def count():
        entered.set()
        await asyncio.Event().wait()

    writer = StatusFileWriter(
        state_machine=ResilienceStateMachine(),
        deferred_queue=SimpleNamespace(count_pending=count), path=str(tmp_path / "status.json"),
    )
    pending = asyncio.create_task(writer.write())
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    writer._deferred_queue = None
    await asyncio.wait_for(writer.write(), 2)
    await writer.aclose()
    assert writer._path.exists()


async def test_serialization_and_submit_failure_allow_retry(monkeypatch, tmp_path, caplog):
    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), path=str(tmp_path / "status.json"))
    writer.set_extra_data("bad", object())
    await writer.write()
    assert "serialization FAILED" in caplog.text
    writer.set_extra_data("bad", None)
    loop = asyncio.get_running_loop()
    with monkeypatch.context() as patch:
        def refuse(*args):
            raise RuntimeError("executor unavailable")

        patch.setattr(loop, "run_in_executor", refuse)
        with pytest.raises(RuntimeError, match="executor unavailable"):
            await writer.write()
    await asyncio.wait_for(writer.write(), 2)
    await writer.aclose()
    assert json.loads(writer._path.read_text())["bad"] is None


async def test_late_worker_exception_is_observed(monkeypatch, tmp_path, caplog):
    entered, release = threading.Event(), threading.Event()

    def fail(path, payload):
        entered.set()
        assert release.wait(5)
        raise OSError("late directory failure")

    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), path=str(tmp_path / "status.json"))
    monkeypatch.setattr(StatusFileWriter, "_publish", staticmethod(fail))
    pending = asyncio.create_task(writer.write())
    await spin_until(entered.is_set)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    await writer.aclose()
    assert "late directory failure" in caplog.text
    assert writer._publication is None


@pytest.mark.parametrize("operation", ["mkstemp", "fdopen", "write", "close", "replace"])
async def test_failed_publication_preserves_old_file(monkeypatch, tmp_path, caplog, operation):
    path = tmp_path / "status.json"
    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), path=str(path))
    await writer.write()
    previous = path.read_bytes()
    original = os.fdopen
    opened_fds = []

    def fail(*args, **kwargs):
        raise OSError("injected " + operation)

    def fdopen(fd, *args, **kwargs):
        opened_fds.append(fd)
        if operation == "fdopen":
            return fail()
        handle = original(fd, *args, **kwargs)

        class Handle:
            def __enter__(self):
                return self

            def write(self, data):
                if operation == "write":
                    handle.write(data[:4])
                    return fail()
                return handle.write(data)

            def __exit__(self, *exc):
                handle.close()
                if operation == "close":
                    fail()

        return Handle()

    with monkeypatch.context() as patch:
        if operation == "mkstemp":
            patch.setattr(tempfile, "mkstemp", fail)
        elif operation == "replace":
            patch.setattr(os, "replace", fail)
        else:
            patch.setattr(os, "fdopen", fdopen)
        await writer.write()
    assert "injected " + operation in caplog.text
    assert path.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    await writer.write()
    await writer.aclose()


async def test_buffered_publication_handles_short_raw_writes(monkeypatch, tmp_path):
    real_write = os.write
    raw_calls = []

    class ShortWriter(io.RawIOBase):
        def __init__(self, fd):
            self.fd = fd

        def writable(self):
            return True

        def write(self, data):
            raw_calls.append(len(data))
            return real_write(self.fd, data[:7])

        def close(self):
            if not self.closed:
                os.close(self.fd)
            super().close()

    monkeypatch.setattr(os, "fdopen", lambda fd, mode: io.BufferedWriter(ShortWriter(fd)))
    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), path=str(tmp_path / "status.json"))
    writer.set_extra_data("payload", "abc" * 4000)
    await writer.write()
    assert len(raw_calls) > 1
    assert json.loads(writer._path.read_text())["payload"] == "abc" * 4000
    assert writer._path.stat().st_mode & 0o777 == 0o600
    await writer.aclose()


async def test_directory_failure_propagates_and_allows_retry(monkeypatch, tmp_path):
    path = tmp_path / "status.json"
    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), path=str(path))
    mkdir = Path.mkdir

    def fail(directory, *args, **kwargs):
        if directory == path.parent:
            raise OSError("directory unavailable")
        return mkdir(directory, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", fail)
        with pytest.raises(OSError, match="directory unavailable"):
            await writer.write()
    await writer.write()
    await writer.aclose()
    assert path.exists()


async def test_error_reporting_failure_does_not_strand_admission(monkeypatch, tmp_path):
    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), path=str(tmp_path / "status.json"))
    publish = StatusFileWriter._publish
    callback_errors = []
    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: callback_errors.append(context))

    def fail(*args, **kwargs):
        raise OSError("reporting unavailable")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(StatusFileWriter, "_publish", staticmethod(fail))
            patch.setattr("genesis.resilience.status_writer.logger.error", fail)
            with pytest.raises(OSError, match="reporting unavailable"):
                await writer.write()
        assert callback_errors
        assert StatusFileWriter._publish is publish
        await asyncio.wait_for(writer.write(), 2)
        await writer.aclose()
    finally:
        loop.set_exception_handler(handler)
