"""The invoker side of the self-reported final status (cc/final_status.py).

``report_status=True`` adds the contract to the system prompt and splits the
trailing block off ``CCOutput.text`` on both run paths; without it nothing
changes.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.cc import final_status
from genesis.cc.final_status import FinalStatus
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation
from tests.test_cc.test_invoker import (
    _make_async_stdout,
    _make_mock_stderr,
    _make_mock_stdin,
    _make_stream_lines,
)

BLOCK = '```final_status\n{"status": "blocked", "reason": "a tool was denied"}\n```'
ANSWER = '{"proposals": [], "focus_summary": "x"}'


@pytest.fixture
def invoker():
    return CCInvoker(claude_path="/usr/bin/claude")


def _flag_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


# ── the contract in the system prompt ──────────────────────────────────────


def test_no_contract_unless_asked(invoker):
    args = invoker._build_args(CCInvocation(prompt="hi", system_prompt="You are X."))
    assert final_status.CONTRACT not in " ".join(args)


def test_the_contract_follows_the_callers_prompt_with_its_flag(invoker):
    for append, flag in ((False, "--system-prompt"), (True, "--append-system-prompt")):
        inv = CCInvocation(
            prompt="hi", system_prompt="You are X.", append_system_prompt=append, report_status=True
        )
        args = invoker._build_args(inv)
        assert _flag_value(args, flag) == "You are X.\n\n" + final_status.CONTRACT


def test_with_no_caller_prompt_the_contract_is_appended_never_a_replacement(invoker):
    args = invoker._build_args(CCInvocation(prompt="hi", report_status=True))
    assert "--system-prompt" not in args
    assert _flag_value(args, "--append-system-prompt") == final_status.CONTRACT


def test_protected_paths_text_comes_before_the_contract(invoker):
    invoker._protected_paths = MagicMock()
    invoker._protected_paths.format_for_prompt.return_value = "PROTECTED"
    inv = CCInvocation(
        prompt="hi", system_prompt="You are X.", skip_permissions=True, report_status=True
    )
    prompt = _flag_value(invoker._build_args(inv), "--system-prompt")
    assert prompt == "You are X.\n\nPROTECTED\n\n" + final_status.CONTRACT


def test_protected_paths_still_need_a_caller_prompt(invoker):
    """The contract must not change which sessions get the protected-paths text."""
    invoker._protected_paths = MagicMock()
    invoker._protected_paths.format_for_prompt.return_value = "PROTECTED"
    inv = CCInvocation(prompt="hi", skip_permissions=True, report_status=True)
    assert "PROTECTED" not in _flag_value(invoker._build_args(inv), "--append-system-prompt")


# ── the split on both run paths ────────────────────────────────────────────


def _result(text: str) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": "s1",
        "total_cost_usd": 0.01,
        "duration_ms": 10,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "modelUsage": {"claude-sonnet-4-6": {}},
    }


async def _run(invoker, text: str, **kw):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(json.dumps(_result(text)).encode(), b""))
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        return await invoker.run(CCInvocation(prompt="hi", **kw))


async def _run_streaming(invoker, text: str, **kw):
    proc = AsyncMock()
    proc.stdout = _make_async_stdout(_make_stream_lines(_result(text)))
    proc.stdin = _make_mock_stdin()
    proc.stderr = _make_mock_stderr()
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        return await invoker.run_streaming(CCInvocation(prompt="hi", **kw))


@pytest.mark.parametrize("run", [_run, _run_streaming])
async def test_the_block_is_split_off_the_text(invoker, run):
    output = await run(invoker, ANSWER + "\n\n" + BLOCK, report_status=True)
    assert output.text == ANSWER
    assert output.final_status == FinalStatus("blocked", "a tool was denied")


@pytest.mark.parametrize("run", [_run, _run_streaming])
async def test_without_the_flag_the_text_is_never_touched(invoker, run):
    output = await run(invoker, ANSWER + "\n\n" + BLOCK)
    assert output.text == ANSWER + "\n\n" + BLOCK
    assert output.final_status is None


@pytest.mark.parametrize("run", [_run, _run_streaming])
async def test_no_block_leaves_the_text_and_no_report(invoker, run):
    output = await run(invoker, ANSWER, report_status=True)
    assert output.text == ANSWER
    assert output.final_status is None


@pytest.mark.parametrize("run", [_run, _run_streaming])
async def test_a_session_reading_outside_content_has_its_reason_labeled(invoker, run):
    """Outside text can steer it, so the owner sees it as the session's words."""
    output = await run(
        invoker, ANSWER + "\n\n" + BLOCK, report_status=True, origin="external_untrusted"
    )
    assert output.final_status == FinalStatus("blocked", "the session says: a tool was denied")
    assert output.text == ANSWER


async def test_a_first_party_session_reason_is_not_labeled(invoker):
    output = await _run(invoker, ANSWER + "\n\n" + BLOCK, report_status=True)
    assert output.final_status.reason == "a tool was denied"
