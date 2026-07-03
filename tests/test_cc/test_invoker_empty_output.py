"""Tests for the silent-cap empty-output callback (on_cc_empty_output).

The invoker fires on_cc_empty_output ONLY when an invocation opted in via
expect_output=True returns genuinely-empty output (no text, no error). It must
NOT fire for the default (expect_output=False) or for a non-empty result. The
callback never alters control flow — the (empty) output is still returned.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation


def _result_line(*, result_text: str, is_error: bool = False) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "result": result_text,
            "session_id": "sess-empty-1",
            "total_cost_usd": 0.0,
            "duration_ms": 10,
            "usage": {"input_tokens": 5, "output_tokens": 0},
            "modelUsage": {"claude-sonnet-4-6": {"outputTokens": 0}},
        }
    )


def _mock_proc(stdout: str):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
    proc.returncode = 0
    return proc


@pytest.mark.asyncio
async def test_empty_output_fires_callback_when_expected():
    cb = AsyncMock()
    invoker = CCInvoker(claude_path="/usr/bin/claude", on_cc_empty_output=cb)
    with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(_result_line(result_text=""))):
        output = await invoker.run(CCInvocation(prompt="hi", expect_output=True))
    cb.assert_awaited_once()
    # callback receives (invocation, output); output is the (empty) result, still returned
    inv_arg, out_arg = cb.await_args.args
    assert inv_arg.expect_output is True
    assert out_arg.text == ""
    assert output.text == ""  # control flow unchanged — empty output still returned


@pytest.mark.asyncio
async def test_empty_output_no_callback_when_not_expected():
    cb = AsyncMock()
    invoker = CCInvoker(claude_path="/usr/bin/claude", on_cc_empty_output=cb)
    with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(_result_line(result_text=""))):
        await invoker.run(CCInvocation(prompt="hi"))  # expect_output defaults False
    cb.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonempty_output_no_callback():
    cb = AsyncMock()
    invoker = CCInvoker(claude_path="/usr/bin/claude", on_cc_empty_output=cb)
    with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(_result_line(result_text="Done."))):
        output = await invoker.run(CCInvocation(prompt="hi", expect_output=True))
    cb.assert_not_awaited()
    assert output.text == "Done."


@pytest.mark.asyncio
async def test_whitespace_only_output_counts_as_empty():
    cb = AsyncMock()
    invoker = CCInvoker(claude_path="/usr/bin/claude", on_cc_empty_output=cb)
    with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(_result_line(result_text="  \n\t "))):
        await invoker.run(CCInvocation(prompt="hi", expect_output=True))
    cb.assert_awaited_once()  # not .strip() → whitespace-only is empty


@pytest.mark.asyncio
async def test_callback_failure_never_breaks_the_call():
    cb = AsyncMock(side_effect=RuntimeError("sink down"))
    invoker = CCInvoker(claude_path="/usr/bin/claude", on_cc_empty_output=cb)
    with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(_result_line(result_text=""))):
        output = await invoker.run(CCInvocation(prompt="hi", expect_output=True))
    # callback raised, but run() still returns the output
    assert output.text == ""
