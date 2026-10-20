"""Exercise ownership through the real invoker control flow without host signals."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.cc import invoker as invoker_module
from genesis.cc.invoker import CCInvoker
from genesis.cc.provider_readiness import READY, probe_provider
from genesis.cc.types import CCInvocation
from genesis.util import proc_kill
from genesis.util.proc_kill import ProcessCleanupUncertain
from tests.test_cc.test_invoker import (
    _make_stream_lines,
    _no_host_syscalls,
    _result_event,
    _streaming_proc,
)


@pytest.fixture
def streaming(monkeypatch, tmp_path):
    _no_host_syscalls(monkeypatch)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("GENESIS_BIG_TMP", str(tmp_path))
    monkeypatch.setattr(invoker_module, "_CC_SPAN_SETTINGS_PATH", tmp_path / "span-settings.json")
    monkeypatch.setattr(invoker_module, "_SEALED_GH_CONFIG_DIR", tmp_path / "sealed-gh")
    invoker = CCInvoker(claude_path="synthetic-cli")
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    monkeypatch.setattr(invoker, "_cc_version", AsyncMock(return_value=None))
    monkeypatch.setattr(
        invoker, "_apply_login_fallback", AsyncMock(side_effect=lambda env, inv: env)
    )
    monkeypatch.setattr(
        invoker, "_build_env", lambda inv: dict.fromkeys(invoker_module._GH_CREDENTIAL_ENV, "")
    )
    monkeypatch.setattr(invoker_module, "_get_scope_args", AsyncMock(return_value=[]))
    monkeypatch.setattr(invoker_module, "_settings_env_pins", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        invoker_module,
        "admit_launch",
        AsyncMock(return_value=SimpleNamespace(release=lambda: None)),
    )
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
        _result_event(READY),
    )
    proc = _streaming_proc(data)
    proc.pid = 424215
    monkeypatch.setattr(invoker_module, "spawn_owned", AsyncMock(return_value=proc))
    return invoker, proc


@pytest.mark.parametrize("phase", ["stream", "stdin", "cancel"])
async def test_uncertain_cleanup_keeps_registered_group_and_probe_scratch(
    streaming, monkeypatch, tmp_path, phase
):
    invoker, proc = streaming
    monkeypatch.setattr(
        invoker_module, "confirm_process_exit", AsyncMock(side_effect=ProcessCleanupUncertain())
    )
    if phase == "stdin":
        proc.stdin.write.side_effect = BrokenPipeError("synthetic broken pipe")
    elif phase == "cancel":
        proc.stdin.drain.side_effect = asyncio.CancelledError()
    with pytest.raises(ProcessCleanupUncertain) as raised:
        await probe_provider(invoker, CCInvocation(prompt="synthetic"), timeout_s=7)
    assert raised.value.process is proc
    assert raised.value.probe_directory.exists()
    assert proc in invoker._active_procs.values()
    invoker._register_proc("next", SimpleNamespace(pid=424216, returncode=None))
    assert proc in invoker._active_procs.values()
    assert invoker._cleanup_owned_procs


async def test_cancelled_launch_callback_retains_uncertain_group(streaming, monkeypatch):
    invoker, proc = streaming
    monkeypatch.setattr(
        invoker_module, "confirm_process_exit", AsyncMock(side_effect=ProcessCleanupUncertain())
    )
    invocation = CCInvocation(
        prompt="synthetic",
        confirm_cleanup=True,
        on_spawn=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(ProcessCleanupUncertain):
        await invoker.run_streaming(invocation)
    assert proc in invoker._active_procs.values()


async def test_interrupted_spawn_transfers_known_handle(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    proc = SimpleNamespace(pid=424215, returncode=0)

    async def create(*args, **kwargs):
        entered.set()
        await release.wait()
        return proc

    monkeypatch.setattr(proc_kill.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        proc_kill, "confirm_process_exit", AsyncMock(side_effect=ProcessCleanupUncertain())
    )
    task = asyncio.create_task(proc_kill.spawn_owned("synthetic-cli"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(ProcessCleanupUncertain) as raised:
        await task
    assert raised.value.process is proc


async def test_invoker_retains_handle_from_interrupted_creation(streaming, monkeypatch):
    invoker, proc = streaming
    monkeypatch.setattr(
        invoker_module, "spawn_owned", AsyncMock(side_effect=ProcessCleanupUncertain(process=proc))
    )
    with pytest.raises(ProcessCleanupUncertain):
        await probe_provider(invoker, CCInvocation(prompt="synthetic"), timeout_s=7)
    assert proc in invoker._active_procs.values()
    assert invoker._cleanup_owned_procs
