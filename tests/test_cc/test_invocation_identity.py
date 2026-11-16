"""Reported CLI identity is advisory metadata, separate from Genesis sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock

import pytest

from genesis.cc import invoker as invoker_mod
from genesis.cc.exceptions import (
    CCError,
    CCMCPError,
    CCOverloadedError,
    CCProcessError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCReplayUnsafeError,
    CCSessionError,
)
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation, CCOutput
from genesis.observability import spans
from genesis.observability.session_context import session_scope
from genesis.util.execution_privacy import private_execution

SID = "synthetic-cli-identity"
ID_CASES = [
    (SID, SID),
    (" opaque identity ", " opaque identity "),
    ("", None),
    (None, None),
    (0, None),
    (True, None),
    ({}, None),
    ([], None),
    (" \t", None),
]
BRANCHES = [
    ("ordinary failure", None, 1, CCProcessError),
    ("session expired", None, 1, CCSessionError),
    ("usage limit reached", None, 1, CCQuotaExhaustedError),
    ("rate limit", 429, 1, CCRateLimitError),
    ("mcp server unavailable", None, 1, CCMCPError),
    ("overloaded", 529, 1, CCOverloadedError),
    ("overloaded", 529, 2, CCReplayUnsafeError),
    ("thinking blocks cannot be modified", None, 1, CCSessionError),
]


@pytest.mark.parametrize("source", ["stdout", "result"])
@pytest.mark.parametrize("value,expected", ID_CASES)
@pytest.mark.parametrize("text,status,turns,kind", BRANCHES)
def test_classifier_retains_only_structured_identity(
    source, value, expected, text, status, turns, kind
):
    payload = {"type": "result", "result": text, "session_id": value, "num_turns": turns}
    if status is not None:
        payload["api_error_status"] = status
    kwargs = {"stdout_text": json.dumps(payload)} if source == "stdout" else {"result": payload}
    err = CCInvoker._classify_error("diagnostic", **kwargs)
    assert type(err) is kind
    assert err.args == ("diagnostic",)
    assert getattr(err, "cc_session_id", None) == expected
    if status == 529:
        assert err.raw_event == payload
        if turns > 1:
            assert type(err.__cause__) is CCOverloadedError
            assert err.__cause__.num_turns == turns


@pytest.mark.parametrize("override", [{"type": "result"}, {"type": "other", "session_id": SID}])
def test_explicit_result_never_borrows_stdout_identity(override):
    err = CCInvoker._classify_error(
        "ordinary",
        json.dumps({"type": "result", "session_id": SID}),
        result=override,
    )
    assert getattr(err, "cc_session_id", None) is None


def test_prose_and_manual_errors_are_identity_unknown():
    err = CCInvoker._classify_error(json.dumps({"type": "result", "session_id": SID}))
    assert getattr(err, "cc_session_id", None) is None
    assert CCError("ordinary").cc_session_id is None


def test_same_attempt_wrappers_keep_identity_without_copying_new_raw_data():
    cause = CCProcessError("ordinary")
    cause.cc_session_id = SID
    wrapped = CCReplayUnsafeError("do not replay", cause=cause)
    assert getattr(wrapped, "cc_session_id", None) == SID
    assert wrapped.__cause__ is cause
    dropped = invoker_mod._unreplayable_after_drop(1, "error result", cc_session_id=SID)
    assert dropped.cc_session_id == SID
    assert dropped.raw_event is None and dropped.raw_text is None


@pytest.fixture
def captured(monkeypatch):
    records = []

    class Writer:
        def record(self, span):
            records.append(span)

    monkeypatch.setattr(spans, "_writer", Writer())
    monkeypatch.setattr(spans, "_enabled", True)
    return records


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "mode", ["success", "error", "cancel", "postprocess", "postprocess_cc", "missing"]
)
@pytest.mark.parametrize("scope", ["ordinary", "private", "disabled"])
async def test_traced_outcome_identity_and_namespace(captured, monkeypatch, streaming, mode, scope):
    invoker = CCInvoker(claude_path="/no-cli-is-run")
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    error = asyncio.CancelledError() if mode == "cancel" else CCProcessError("synthetic failure")
    if mode == "error":
        error.cc_session_id = SID
    output = CCOutput(
        session_id="" if mode == "missing" else SID,
        text="synthetic",
        model_used="sonnet",
        cost_usd=0,
        input_tokens=0,
        output_tokens=0,
        duration_ms=1,
        exit_code=0,
    )

    async def inner(*args):
        if mode in ("error", "cancel"):
            raise error
        return output

    monkeypatch.setattr(invoker, "_run_streaming_inner" if streaming else "_run_inner", inner)
    if mode in ("postprocess", "postprocess_cc"):
        error = (
            CCProcessError("formatter failed")
            if mode == "postprocess_cc"
            else RuntimeError("formatter failed")
        )

        def split(*args):
            raise error

        monkeypatch.setattr(invoker_mod, "_split_final_status", split)
    if scope == "disabled":
        monkeypatch.setattr(spans, "_enabled", False)
    method = invoker._run_streaming_traced if streaming else invoker._run_traced
    with (
        session_scope("genesis-manager-fixture"),
        private_execution() if scope == "private" else contextlib.nullcontext(),
    ):
        if mode in ("error", "cancel", "postprocess", "postprocess_cc"):
            with pytest.raises(type(error)) as caught:
                await method(CCInvocation(prompt="synthetic"), "fixture")
            assert caught.value is error
        else:
            result = await method(CCInvocation(prompt="synthetic"), "fixture")
            assert result.session_id == output.session_id and result.text == output.text
    if scope == "disabled":
        assert captured == []
        return
    assert len(captured) == 1
    record = captured[0]
    assert record.session_id == "genesis-manager-fixture"
    actual = {k: v for k, v in record.attributes.items() if k.startswith("cc_")}
    if scope == "private":
        assert actual == {}
    else:
        known = mode not in ("cancel", "missing")
        assert actual == {
            "cc_session_id": SID if known else None,
            "cc_identity_source": "classified_result"
            if mode == "error"
            else "cc_output"
            if known
            else "unknown",
            "cc_outcome": "returned" if mode in ("success", "missing") else "not_returned",
        }


def test_categorical_span_never_formats_error_and_rethrows_same_object(captured):
    class Unformattable(Exception):
        def __str__(self):
            raise AssertionError("must not format this error")

    error = Unformattable()
    try:
        with spans.start_span("inbox.evaluate", include_exception_text=False):
            raise error
    except BaseException as caught:
        assert caught is error
    assert len(captured) == 1
    assert captured[0].status == "error"
    assert "must not format" not in captured[0].status_message


def test_default_span_error_format_stays_compatible(captured):
    error = ValueError("synthetic message")
    with pytest.raises(ValueError) as caught, spans.start_span("ordinary"):
        raise error
    assert caught.value is error
    assert captured[0].status_message == "ValueError: synthetic message"


def test_categorical_span_status_fault_keeps_original(captured, monkeypatch):
    def broken(*args):
        raise RuntimeError("synthetic status failure")

    monkeypatch.setattr(spans.Span, "set_status_error", broken)
    original = ValueError("original")
    try:
        with spans.start_span("inbox.evaluate", include_exception_text=False):
            raise original
    except BaseException as caught:
        assert caught is original


async def test_metadata_setter_failure_does_not_replace_error(captured, monkeypatch):
    def broken(*args):
        raise RuntimeError("synthetic setter failure")

    monkeypatch.setattr(spans.Span, "set_attr", broken)
    invoker = CCInvoker(claude_path="/no-cli-is-run")
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    error = CCProcessError("original")
    error.cc_session_id = SID
    monkeypatch.setattr(invoker, "_run_inner", AsyncMock(side_effect=error))
    with pytest.raises(CCProcessError) as caught:
        await invoker._run_traced(CCInvocation(prompt="synthetic"), "fixture")
    assert caught.value is error
