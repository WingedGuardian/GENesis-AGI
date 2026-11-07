"""Readiness observes tool authority, not just an instruction to avoid tools."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.cc.exceptions import CCProcessError, CCQuotaExhaustedError, CCRateLimitError
from genesis.cc.invoker import CCInvoker
from genesis.cc.provider_readiness import READY, isolated_provider, probe_provider
from genesis.cc.types import CCInvocation, CCOutput, StreamEvent
from genesis.util.proc_kill import ProcessCleanupUncertain


def answer(**kwargs):
    output = CCOutput("probe", READY, "sonnet", 0, 1, 1, 1, 0, tools_used=())
    return replace(output, **kwargs)


def init(**kwargs):
    return StreamEvent(
        "init", raw={"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], **kwargs}
    )


@pytest.fixture(autouse=True)
def isolated_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_BIG_TMP", str(tmp_path))


async def test_probe_removes_task_authority_and_observes_it(tmp_path):
    async def run(invocation, on_event):
        assert invocation.prompt != "private task"
        assert invocation.safe_mode and invocation.disable_tools and not invocation.bare
        assert invocation.confirm_cleanup and not invocation.supervised
        assert invocation.no_session_persistence
        assert invocation.system_prompt is None and invocation.resume_session_id is None
        assert Path(invocation.mcp_config).read_text() == '{"mcpServers":{}}'
        assert invocation.timeout_s == 7
        await on_event(init())
        return answer()

    await probe_provider(
        SimpleNamespace(run_streaming=run), CCInvocation(prompt="private task"), timeout_s=7
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("event", [None, init(tools=["Bash"]), init(mcp_servers=[{}])])
async def test_missing_or_nonempty_authority_fails(event):
    async def run(invocation, on_event):
        if event:
            await on_event(event)
        return answer()

    with pytest.raises(CCProcessError):
        await probe_provider(
            SimpleNamespace(run_streaming=run), CCInvocation(prompt="x"), timeout_s=7
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"text": ""},
        {"is_error": True},
        {"result_received": False},
        {"bg_truncated": True},
        {"stream_lines_dropped": 1},
        {"tools_used": None},
        {"tools_used": ("Bash",)},
    ],
)
async def test_partial_or_unobserved_answers_do_not_establish_readiness(changes):
    async def run(invocation, on_event):
        await on_event(init())
        return answer(**changes)

    with pytest.raises(CCProcessError):
        await probe_provider(
            SimpleNamespace(run_streaming=run), CCInvocation(prompt="x"), timeout_s=7
        )


async def test_uncertain_cleanup_retains_directory(tmp_path):
    invoker = SimpleNamespace(run_streaming=AsyncMock(side_effect=ProcessCleanupUncertain()))
    with pytest.raises(ProcessCleanupUncertain):
        await probe_provider(invoker, CCInvocation(prompt="x"), timeout_s=7)
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize(
    "infos,refused",
    [
        ([{"status": "allowed", "overageStatus": "rejected", "isUsingOverage": False}], False),
        ([{"status": "allowed_warning", "isUsingOverage": False}], False),
        ([{"status": "rejected", "isUsingOverage": False}], True),
        ([{"status": "rejected"}], True),
        ([{"status": "rejected", "overageStatus": "allowed", "isUsingOverage": True}], False),
        (
            [{"status": "rejected", "overageStatus": "allowed_warning", "isUsingOverage": True}],
            False,
        ),
        ([{"status": "rejected", "overageStatus": "allowed", "isUsingOverage": False}], True),
        ([{"status": "allowed"}, {"status": "rejected", "isUsingOverage": False}], True),
        ([{"status": "rejected"}, {"status": "allowed"}], False),
        ([{"status": "rejected"}, {"status": "allowed_warning"}], False),
    ],
)
async def test_readiness_uses_latest_effective_limit_status(infos, refused):
    async def run(invocation, on_event):
        await on_event(init())
        for info in infos:
            await on_event(
                StreamEvent(
                    "rate_limit_event", raw={"type": "rate_limit_event", "rate_limit_info": info}
                )
            )
        return answer()

    invoke = probe_provider(
        SimpleNamespace(run_streaming=run), CCInvocation(prompt="x"), timeout_s=7
    )
    if refused:
        with pytest.raises(CCRateLimitError):
            await invoke
    else:
        assert (await invoke).text == READY


async def test_probe_status_isolation_is_task_local_and_does_not_clear_prior_failure():
    status = AsyncMock()
    invoker = CCInvoker(on_cc_status_change=status)
    await invoker._notify_status_change(CCQuotaExhaustedError())
    with isolated_provider(probe=True):
        await invoker._notify_status_change(None)
    assert invoker._last_was_error
    assert status.await_count == 1
    await invoker._notify_status_change(None)
    assert not invoker._last_was_error


async def test_isolated_provider_does_not_suppress_another_tasks_status():
    status = AsyncMock()
    invoker = CCInvoker(on_cc_status_change=status)
    entered, release = asyncio.Event(), asyncio.Event()

    async def peer_call():
        with isolated_provider():
            entered.set()
            await release.wait()
            await invoker._notify_status_change(None)

    peer = asyncio.create_task(peer_call())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await invoker._notify_status_change(CCQuotaExhaustedError())
        status.assert_awaited_once()
        release.set()
        await peer
        status.assert_awaited_once()
        assert invoker._last_was_error
    finally:
        release.set()
        await peer
