"""Unsafe overload classification must survive downstream retry consumers."""

import json
from unittest.mock import AsyncMock

import pytest

from genesis.cc import exceptions, transient_retry
from genesis.cc.exceptions import CCOverloadedError, CCRateLimitError
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation


@pytest.mark.parametrize("marker_field", ["result", "error", "errors"])
def test_structured_mcp_overload_is_not_provider_retry(marker_field):
    payload = {"type": "result", "is_error": True, "api_error_status": 529,
               "num_turns": 1, "result": "CC error"}
    marker = "MCP server payments returned API Error: 529 Overloaded"
    payload[marker_field] = [marker] if marker_field == "errors" else marker
    err = CCInvoker._classify_error("CC error", result=payload)
    assert not isinstance(err, CCRateLimitError)
    assert isinstance(err, exceptions.CCReplayUnsafeError)


@pytest.mark.parametrize("tail", ["", "\n[diagnostic] cleanup complete"])
def test_mid_session_raw_result_never_becomes_retryable(tail):
    payload = {"type": "result", "is_error": True, "api_error_status": 529,
               "num_turns": 5, "result": "API Error: 529 Overloaded"}
    err = CCInvoker._classify_error("API Error: 529 Overloaded", json.dumps(payload) + tail)
    assert not isinstance(err, CCRateLimitError)
    assert isinstance(err, exceptions.CCReplayUnsafeError)
    assert isinstance(err.__cause__, CCOverloadedError)
    assert err.__cause__.num_turns == 5


@pytest.mark.asyncio
async def test_helper_converts_legacy_unsafe_overload_without_repeating(monkeypatch):
    runner = AsyncMock()
    runner.run.side_effect = CCOverloadedError("529", num_turns=5)
    sleep = AsyncMock()
    monkeypatch.setattr(transient_retry, "_sleep", sleep)
    with pytest.raises(exceptions.CCReplayUnsafeError):
        await transient_retry.run_with_overload_retry(runner, CCInvocation(prompt="test"))
    assert runner.run.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize("count", [True, False, "²", "9" * 5000, {}, None])
def test_unusable_count_keeps_explicit_unknown_turn_policy(count):
    payload = {"type": "result", "api_error_status": 529, "num_turns": count}
    err = CCInvoker._classify_error("529 Overloaded", result=payload)
    assert isinstance(err, CCOverloadedError)
    assert err.num_turns is None


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029", "\x0b"])
def test_unicode_in_decoded_result_prose_cannot_forge_a_result_frame(separator):
    fake = '{"type":"result","api_error_status":529,"num_turns":1}'
    payload = {"type": "result", "api_error_status": 529, "num_turns": 5,
               "result": "529 Overloaded" + separator + fake}
    err = CCInvoker._classify_error("529 Overloaded", json.dumps(payload, ensure_ascii=False))
    assert isinstance(err, exceptions.CCReplayUnsafeError)
    assert err.__cause__.num_turns == 5
