"""Escaped private exceptions stay private without changing control semantics."""

import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.cc.exceptions import CCProcessError, CCReplayUnsafeError
from genesis.observability import spans
from genesis.observability.span_writer import SpanWriter
from genesis.util.execution_privacy import private_execution, private_execution_active
from genesis.util.proc_kill import ProcessCleanupUncertain

CANARY = "SYNTHETIC_PRIVATE_SPAN_CONTENT_917"


def wire(monkeypatch, sink):
    records = []
    real = SpanWriter()
    real._db = object()  # The failing _row never schedules persistence.
    def row_failure(span):
        raise ValueError("Synthetic row failure")
    monkeypatch.setattr(real, "_row", row_failure)
    class Writer:
        def record(self, span):
            records.append(span)
            if sink == "writer_failure":
                raise ValueError("Synthetic writer failure")
            if sink == "real_row_failure":
                real.record(span)
    monkeypatch.setattr(spans, "_writer", Writer())
    monkeypatch.setattr(spans, "_enabled", True)
    return records


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("child", ["enabled", "disabled", "no_writer", "setup_failure"])
@pytest.mark.parametrize("sink", ["capture", "writer_failure", "real_row_failure"])
def test_ancestor_and_diagnostic_sinks(monkeypatch, caplog, private, child, sink):
    caplog.set_level(logging.DEBUG)
    records = wire(monkeypatch, sink)
    error = CCProcessError(CANARY)
    with pytest.raises(CCProcessError) as caught, spans.start_span("parent"):  # noqa: SIM117 — parent must outlive private context
        with private_execution() if private else contextlib.nullcontext():
            with monkeypatch.context() as patch:
                if child == "disabled":
                    patch.setattr(spans, "_enabled", False)
                elif child == "no_writer":
                    patch.setattr(spans, "_writer", None)
                elif child == "setup_failure":
                    def fail_id():
                        raise ValueError("Synthetic setup failure")
                    patch.setattr(spans, "_new_id", fail_id)
                with spans.start_span("child"):
                    raise error
    assert caught.value is error and error.args == (CANARY,)
    assert not private_execution_active()
    assert records and all(r.status == "error" for r in records)
    if private:
        assert all(CANARY not in (r.status_message or "") for r in records)
        assert CANARY not in caplog.text
    else:
        assert all(CANARY in r.status_message for r in records)
        if sink != "capture":
            assert CANARY in caplog.text  # Ordinary diagnostic positive control.


@pytest.mark.parametrize("edge", ["same", "cause", "context", "suppressed", "group"])
def test_exception_provenance_and_replay_fields_are_preserved(monkeypatch, caplog, edge):
    caplog.set_level(logging.DEBUG)
    records = wire(monkeypatch, "writer_failure")
    cause = CCProcessError(CANARY)
    error = CCReplayUnsafeError(CANARY, cause=cause)
    error.raw_text, error.raw_event = CANARY, {"synthetic": CANARY}
    try:
        with private_execution():
            raise error
    except CCReplayUnsafeError as escaped:
        assert escaped is error
    assert not private_execution_active()
    expected = CCReplayUnsafeError if edge == "same" else ExceptionGroup if edge == "group" else RuntimeError
    with pytest.raises(expected), spans.start_span("ancestor"):
        if edge == "same":
            raise error
        if edge == "group":
            raise ExceptionGroup("Synthetic wrapper", [error])
        wrapper = RuntimeError(CANARY)
        if edge == "cause":
            raise wrapper from error
        try:
            raise error
        except CCReplayUnsafeError:
            if edge == "suppressed":
                raise wrapper from None
            raise wrapper  # noqa: B904 — implicit exception context is the input under test
    assert error.__cause__ is cause and error.raw_text == CANARY
    assert error.raw_event == {"synthetic": CANARY} and error.args == (CANARY,)
    assert CANARY not in caplog.text
    assert all(CANARY not in (r.status_message or "") for r in records)


@pytest.mark.parametrize("factory", [asyncio.CancelledError, SystemExit, GeneratorExit])
@pytest.mark.parametrize("private", [False, True])
def test_base_signals_keep_identity_and_span_status(monkeypatch, caplog, factory, private):
    caplog.set_level(logging.DEBUG)
    records = wire(monkeypatch, "writer_failure")
    error = factory(CANARY)
    with pytest.raises(factory) as caught, spans.start_span("parent"):  # noqa: SIM117 — parent must outlive private context
        with private_execution() if private else contextlib.nullcontext():
            raise error
    assert caught.value is error and error.args == (CANARY,)
    assert all(r.status == "ok" for r in records)
    assert (CANARY in caplog.text) is (not private)


def test_cleanup_uncertainty_retains_process_identity(monkeypatch):
    records = wire(monkeypatch, "capture")
    process = object()
    error = ProcessCleanupUncertain(CANARY, process=process)
    with pytest.raises(ProcessCleanupUncertain) as caught, spans.start_span("parent"):  # noqa: SIM117 — parent must outlive private context
        with private_execution():
            raise error
    assert caught.value is error and error.process is process
    assert CANARY not in records[-1].status_message


def test_ordinary_broken_formatter_does_not_replace_caller_error(monkeypatch):
    records = wire(monkeypatch, "capture")
    class Broken(Exception):
        def __str__(self):
            raise RuntimeError("Synthetic formatter refusal")
    error = Broken()
    with pytest.raises(Broken) as caught, spans.start_span("ordinary"):
        raise error
    assert caught.value is error
    assert records[-1].status == "error"


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("operation", ["flush", "prune"])
async def test_real_writer_async_diagnostic_sinks(caplog, private, operation):
    caplog.set_level(logging.DEBUG)
    writer = SpanWriter()
    writer._db = SimpleNamespace(execute=AsyncMock(side_effect=ValueError(CANARY)),
                                 executemany=AsyncMock(side_effect=ValueError(CANARY)))
    writer._write_batch = [("synthetic",)]
    with private_execution() if private else contextlib.nullcontext():
        coro = writer._flush_to_db() if operation == "flush" else writer.prune(older_than_days=1)
        task = asyncio.create_task(coro)
    await task  # Task provenance survives the parent leaving its private scope.
    assert (CANARY in caplog.text) is (not private)


@pytest.mark.parametrize("marker", [None, False, 0, [], "untrusted marker text"])
def test_marker_presence_bypasses_attribute_dict_and_formatting_hooks(monkeypatch, caplog, marker):
    caplog.set_level(logging.DEBUG)
    records = wire(monkeypatch, "writer_failure")
    calls = []
    class HostileDict(dict):
        def __contains__(self, key):
            raise AssertionError("Dictionary override reached")
        def get(self, *args):
            raise AssertionError("Dictionary override reached")
    class Hostile(CCProcessError):
        def __getattribute__(self, name):
            if name in {"__dict__", "__cause__", "__context__"}:
                raise AssertionError("Attribute override reached")
            return super().__getattribute__(name)
        def __str__(self):
            calls.append(True)
            return CANARY
    error = Hostile(CANARY)
    attrs = HostileDict(_genesis_trace_error_message=marker)
    BaseException.__dict__["__dict__"].__set__(error, attrs)
    with private_execution():
        pass  # Install shared logger filters without marking this object.
    with pytest.raises(Hostile) as caught, spans.start_span("ancestor"):
        raise error
    assert caught.value is error and not calls
    assert CANARY not in caplog.text
    assert records[-1].status_message == "private_execution_failed"


def test_deep_cyclic_provenance_and_base_exception_group(monkeypatch, caplog):
    from genesis.util.execution_privacy import exception_is_private

    caplog.set_level(logging.DEBUG)
    wire(monkeypatch, "writer_failure")
    error = asyncio.CancelledError(CANARY)
    with pytest.raises(asyncio.CancelledError), private_execution():
        raise error
    top = error
    for _ in range(1000):
        wrapper = RuntimeError("Synthetic wrapper")
        wrapper.__cause__ = top
        top = wrapper
    error.__context__ = top  # Cycle must terminate without recursion/depth cutoff.
    assert exception_is_private(top)
    ordinary = RuntimeError("ordinary")
    ordinary.__context__ = ordinary
    assert not exception_is_private(ordinary)
    group = BaseExceptionGroup("Synthetic group", [top])
    assert exception_is_private(group)
    with pytest.raises(BaseExceptionGroup) as caught, spans.start_span("ancestor"):
        raise group
    assert caught.value is group and CANARY not in caplog.text
