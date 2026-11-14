"""Freeze terminal evidence before owned cleanup; do not fabricate pressure."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.cc import invoker as inv_mod
from genesis.cc import oom
from genesis.cc.exceptions import CCMemoryPressureError, CCTimeoutError
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["result", "survivor", "timeout", "cancel"])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_owned_cleanup_never_becomes_pressure(tmp_path, monkeypatch, finish, confirmed):
    events = tmp_path / "memory.events"
    events.write_text("oom_kill 4\n")
    baseline = oom._sample(events)
    events.write_text("oom_kill 5\n")  # unrelated sibling OOM in the same interval
    monkeypatch.setattr(oom, "capture_baseline", lambda **kw: baseline)
    monkeypatch.setattr(inv_mod, "_get_scope_args", AsyncMock(return_value=[]))
    monkeypatch.setattr(inv_mod, "set_oom_score_adj", lambda *a: None)
    stdout = asyncio.StreamReader()
    if finish == "result":
        stdout.feed_data((json.dumps({"type": "result", "result": "done", "is_error": False}) + "\n").encode())
    stdout.feed_eof()
    if finish in ("timeout", "cancel"):
        stdout.readline = AsyncMock(side_effect=TimeoutError if finish == "timeout" else asyncio.CancelledError)
    stderr = asyncio.StreamReader()
    stderr.feed_eof()
    proc = SimpleNamespace(pid=424209, returncode=None, stdout=stdout, stderr=stderr, stdin=None)
    proc.wait = AsyncMock(return_value=None)
    proc.terminate = lambda: setattr(proc, "returncode", -9)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(inv_mod, "spawn_owned", AsyncMock(return_value=proc))
    monkeypatch.setattr(inv_mod, "kill_process_group", lambda p: setattr(p, "returncode", -9))
    monkeypatch.setattr(inv_mod, "process_group_alive", lambda p: p.returncode is None)
    invoker = CCInvoker(claude_path="synthetic")
    invoker._with_cost_semantics = AsyncMock(side_effect=lambda output: output)
    invoker._fire_downgrade_callback = AsyncMock()
    async def confirm_exit(process, key, **kw):
        process.returncode = -9
        invoker._unregister_proc(key)
    invoker._confirm_registered_exit = confirm_exit
    invocation = CCInvocation(prompt="owned fixture", confirm_cleanup=confirmed)
    run = invoker._run_streaming_inner_with_admission(
        invocation, None, SimpleNamespace(release=lambda: None),
    )
    if finish in ("timeout", "cancel"):
        with pytest.raises(CCTimeoutError if finish == "timeout" else asyncio.CancelledError):
            await run
    else:
        output = await run
        assert output.text == ("done" if finish == "result" else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_buffered_owned_kill_preserves_timeout_and_cancellation(tmp_path, monkeypatch, cancel):
    events = tmp_path / "memory.events"
    events.write_text("oom_kill 4\n")
    baseline = oom._sample(events)
    events.write_text("oom_kill 5\n")
    monkeypatch.setattr(oom, "capture_baseline", lambda **kw: baseline)
    monkeypatch.setattr(inv_mod, "_get_scope_args", AsyncMock(return_value=[]))
    monkeypatch.setattr(inv_mod, "set_oom_score_adj", lambda *a: None)
    proc = SimpleNamespace(pid=424210, returncode=None, stderr=None)
    proc.communicate = AsyncMock(side_effect=asyncio.CancelledError if cancel else TimeoutError)
    proc.wait = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(inv_mod, "kill_process_group", lambda p: setattr(p, "returncode", -9))
    invoker = CCInvoker(claude_path="synthetic")
    with pytest.raises(asyncio.CancelledError if cancel else CCTimeoutError):
        await invoker._run_inner_with_admission(
            CCInvocation(prompt="owned fixture"), SimpleNamespace(release=lambda: None),
        )


@pytest.mark.asyncio
async def test_owned_native_sigkill_reaches_pressure_boundary(tmp_path, monkeypatch):
    """Real owned child, synthetic counter: prove wiring without inducing OOM."""
    import sys

    events = tmp_path / "memory.events"
    events.write_text("oom_kill 4\n")
    baseline = oom._sample(events)
    monkeypatch.setattr(oom, "capture_baseline", lambda **kw: baseline)
    monkeypatch.setattr(inv_mod, "_get_scope_args", AsyncMock(return_value=[]))
    monkeypatch.setattr(inv_mod, "set_oom_score_adj", lambda *a: None)
    invoker = CCInvoker(claude_path=sys.executable)
    # No model, service or unowned process is launched. The child only changes
    # its fixture file and self-signals; the counter is NOT real OOM evidence.
    code = "import os,signal,sys; open(sys.argv[1],'w').write('oom_kill 5\\n'); os.kill(os.getpid(),signal.SIGKILL)"
    monkeypatch.setattr(invoker, "_build_args", lambda *a, **kw: [sys.executable, "-c", code, str(events)])
    with pytest.raises(CCMemoryPressureError) as raised:
        await invoker._run_inner_with_admission(
            CCInvocation(prompt="owned fixture"), SimpleNamespace(release=lambda: None),
        )
    assert raised.value.evidence.returncode == -9
    assert raised.value.evidence.after == 5
