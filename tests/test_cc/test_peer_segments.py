"""Peer execution opts in to containment without changing legacy invocation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc import invoker as invoker_module
from genesis.cc import peer_segment
from genesis.cc.exceptions import CCProcessError
from genesis.cc.invoker import CCInvoker
from genesis.cc.peer_segment import PeerSegment
from genesis.cc.types import CCInvocation, CCOutput
from genesis.observability import spans


@pytest.fixture
def peer_invocation(tmp_path):
    config = tmp_path / "facade.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "genesis_peer": {
                        "command": sys.executable,
                        "args": [
                            "-m",
                            "genesis.peers.facade",
                            "--lease-file",
                            str(tmp_path / "lease.json"),
                        ],
                    }
                }
            }
        )
    )
    config.chmod(0o600)
    return CCInvocation(
        prompt="external request",
        system_prompt="Explicit Genesis identity and authorized context.",
        working_dir=str(tmp_path),
        mcp_config=str(config),
        origin="external_untrusted",
        peer_segment=PeerSegment(
            segment_id="a" * 32,
            deadline_at=time.time() + 60,
            tools=("mcp__genesis_peer__perform",),
        ),
    )


def test_peer_arguments_exclude_ambient_customizations(peer_invocation, monkeypatch):
    # A peer launch must never prepare the owner dispatch-hook settings file.
    def forbidden(*args, **kwargs):
        raise AssertionError("owner settings builder reached from peer invocation")

    monkeypatch.setattr("genesis.cc.invoker.cc_span_settings_path", forbidden)
    args = CCInvoker(claude_path="/fixture/claude")._build_args(peer_invocation)
    for flag in ("--tools", "--setting-sources"):
        assert args[args.index(flag) + 1] == ""
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--allowedTools") + 1] == "mcp__genesis_peer__perform"
    assert "--strict-mcp-config" in args
    assert "--disable-slash-commands" in args
    assert "--no-chrome" in args
    assert "--no-session-persistence" in args
    assert "--dangerously-skip-permissions" not in args


@pytest.mark.parametrize(
    "changes",
    [
        {"skip_permissions": True},
        {"resume_session_id": "owner-session"},
        {"strict_mcp_config": False},
        {"bare": True},
        {"safe_mode": True},
        {"supervised": True},
        {"origin": None},
        {"mcp_config": None},
        {"system_prompt": None},
        {"working_dir": None},
        {"allowed_tools": ["Bash"]},
        {"disallowed_tools": ["mcp__genesis_peer__perform"]},
        {"append_system_prompt": True},
        {"bash_allowlist": ("gh",)},
        {"skill_tags": ["owner-skill"]},
        {"env_overrides": {"CLAUDE_CODE_SIMPLE": "1"}},
        {"output_format": "stream-json"},
        {"output_format": "text"},
    ],
)
def test_peer_rejects_unsafe_invocation_overrides(peer_invocation, changes):
    with pytest.raises(ValueError):
        replace(peer_invocation, **changes)


@pytest.mark.parametrize(
    "case",
    ["extra_server", "command", "env", "relative_lease", "public", "symlink", "oversize", "fifo"],
)
def test_launch_rejects_nonfacade_configuration(peer_invocation, tmp_path, case):
    path = tmp_path / "facade.json"
    config = json.loads(path.read_text())
    server = config["mcpServers"]["genesis_peer"]
    if case == "extra_server":
        config["mcpServers"]["owner"] = {"command": "owner"}
    elif case == "command":
        server["command"] = "/bin/sh"
    elif case == "env":
        server["env"] = {"GH_TOKEN": ""}
    elif case == "relative_lease":
        server["args"][-1] = "lease.json"
    if case == "public":
        path.chmod(0o644)
    elif case == "symlink":
        target = tmp_path / "target.json"
        path.rename(target)
        path.symlink_to(target)
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.write_text(" " * 4097 if case == "oversize" else json.dumps(config))
    with pytest.raises(ValueError, match="facade-only configuration"):
        CCInvoker(claude_path="/fixture/claude")._build_args(peer_invocation)


@pytest.mark.parametrize("tool", ["Bash", "mcp__genesis_peer__*", "mcp__owner__read", ""])
def test_peer_tool_names_are_exact_facade_rules(tool):
    with pytest.raises(ValueError):
        PeerSegment(segment_id="a" * 32, deadline_at=time.time() + 60, tools=(tool,))


def test_peer_scope_has_named_lifetime_and_parent(peer_invocation):
    args = peer_invocation.peer_segment.scope_args(("IOWeight=100",))
    assert args[:5] == ["systemd-run", "--user", "--scope", "--collect", "--quiet"]
    assert "--unit=genesis-peer-" + "a" * 32 + ".scope" in args
    for property_value in (
        "IOWeight=100",
        "BindsTo=genesis-server.service",
        "After=genesis-server.service",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "TimeoutStopSec=10s",
    ):
        assert property_value in args
    runtime = next(value for value in args if value.startswith("RuntimeMaxSec="))
    assert 0 < float(runtime.split("=", 1)[1][:-1]) <= 60
    assert args[-1] == "--"


def test_expired_segment_never_spawns():
    peer = PeerSegment(
        segment_id="a" * 32, deadline_at=time.time() - 1, tools=("mcp__genesis_peer__perform",)
    )
    with pytest.raises(ValueError, match="expired"):
        peer.scope_args(())


def test_peer_environment_preserves_provider_auth_without_owner_authority(
    peer_invocation, monkeypatch
):
    for name in ("GENESIS_MCP_HTTP_TOKEN", "GENESIS_DESK_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.setenv(name, secrets.token_urlsafe(32))
    monkeypatch.setenv("ANTHROPIC_API_KEY", secrets.token_urlsafe(32))
    monkeypatch.setenv("GENESIS_SESSION_ID", "owner-session")
    monkeypatch.setenv("GENESIS_SESSION_SUPERVISED", "1")
    monkeypatch.setenv("BASH_ENV", "/fixture/shell-startup")
    env = CCInvoker(claude_path="/fixture/claude")._build_env(peer_invocation)
    assert "ANTHROPIC_API_KEY" in env
    assert env.get("HOME") == os.environ.get("HOME")
    assert env.get("GENESIS_SESSION_ORIGIN") == "external_untrusted"
    assert not {
        "GENESIS_MCP_HTTP_TOKEN",
        "GENESIS_DESK_TOKEN",
        "GENESIS_SESSION_ID",
        "GENESIS_SESSION_SUPERVISED",
        "BASH_ENV",
    }.intersection(env)
    assert all(env[name] == "" for name in invoker_module._GH_CREDENTIAL_ENV)
    CCInvoker._launch_env(env, peer_invocation)


@pytest.mark.parametrize("populated", ["0", "1"])
def test_scope_drain_checks_descendants_not_launcher_pid(
    peer_invocation, tmp_path, monkeypatch, populated
):
    peer = peer_invocation.peer_segment
    root = tmp_path / "cgroup"
    group = root / peer.unit_name
    group.mkdir(parents=True)
    (root / "cgroup.controllers").write_text("memory\n")
    (group / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
    monkeypatch.setattr(peer_segment, "_CGROUP_ROOT", root)
    calls = []

    def systemctl(args, **kwargs):
        calls.append(args)
        if "show" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                f"LoadState=loaded\nActiveState=failed\nControlGroup=/{peer.unit_name}\n",
                "",
            )
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(peer_segment.subprocess, "run", systemctl)
    if populated == "0":
        peer._stop_scope()
    else:
        with pytest.raises(RuntimeError, match="drain was not confirmed"):
            peer._stop_scope()
    assert calls[-1] == ["systemctl", "--user", "stop", peer.unit_name]


def test_failed_manager_query_is_not_an_empty_scope(peer_invocation, monkeypatch):
    monkeypatch.setattr(
        peer_segment.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "bus unavailable"),
    )
    with pytest.raises(RuntimeError, match="state could not be confirmed"):
        peer_invocation.peer_segment._stop_scope()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_public_run_always_drains_peer_scope(
    peer_invocation, monkeypatch, streaming, outcome
):
    monkeypatch.setattr(invoker_module.roster, "apply_active", lambda inv: (inv, ""))
    monkeypatch.setattr(invoker_module, "inflight", lambda *args: contextlib.nullcontext())
    invoker = CCInvoker(claude_path="/fixture/claude")
    error = {
        "success": None,
        "error": RuntimeError("fixture failure"),
        "cancel": asyncio.CancelledError(),
    }[outcome]
    traced = AsyncMock(return_value="complete", side_effect=error)
    monkeypatch.setattr(invoker, "_run_streaming_traced" if streaming else "_run_traced", traced)
    drained = AsyncMock()
    monkeypatch.setattr(PeerSegment, "stop_and_drain", drained)
    call = invoker.run_streaming if streaming else invoker.run
    if error is None:
        assert await call(peer_invocation) == "complete"
    else:
        with pytest.raises(type(error)):
            await call(peer_invocation)
    drained.assert_awaited_once()


@pytest.mark.parametrize("streaming", [False, True])
async def test_private_spawn_callback_error_is_not_logged(
    peer_invocation, monkeypatch, caplog, streaming
):
    async def fail_callback(pid):
        raise RuntimeError("private callback prose")

    invocation = replace(peer_invocation, on_spawn=fail_callback)
    invoker = CCInvoker(claude_path="/fixture/claude")
    monkeypatch.setattr(invoker_module.roster, "apply_active", lambda inv: (inv, ""))
    monkeypatch.setattr(invoker_module, "inflight", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(invoker_module, "set_oom_score_adj", lambda *args: None)
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    monkeypatch.setattr(invoker, "verify_allowlist_enforceable", AsyncMock())
    monkeypatch.setattr(invoker, "_build_env", lambda inv: {})
    monkeypatch.setattr(invoker, "_apply_login_fallback", AsyncMock(side_effect=lambda env, inv: env))
    monkeypatch.setattr(invoker, "_launch_env", lambda env, inv: env)
    monkeypatch.setattr(invoker, "_with_cost_semantics", AsyncMock(side_effect=lambda output: output))
    monkeypatch.setattr(PeerSegment, "stop_and_drain", AsyncMock())
    result = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                         "result": "complete", "session_id": "fixture", "usage": {}}).encode()
    monkeypatch.setattr(invoker_module, "process_group_alive", lambda proc: False)
    monkeypatch.setattr(invoker_module, "kill_process_group", lambda proc: None)
    proc = MagicMock(pid=42000, returncode=0)
    proc.communicate = AsyncMock(return_value=(result, b""))
    proc.wait = AsyncMock(return_value=0)
    proc.stdin.drain = AsyncMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdout.feed_data(result + b"\n")
    proc.stdout.feed_eof()
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_eof()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    call = invoker.run_streaming if streaming else invoker.run
    output = await call(invocation)
    assert output.text == "complete"
    warnings = [record for record in caplog.records if "on_spawn callback failed" in record.message]
    assert len(warnings) == 1
    assert not warnings[0].exc_info
    assert "private callback prose" not in caplog.text


@pytest.mark.parametrize("first_streaming", [False, True])
@pytest.mark.parametrize("second_streaming", [False, True])
@pytest.mark.parametrize("other_loop", [False, True])
async def test_duplicate_invocation_never_drains_owner(
    peer_invocation, monkeypatch, first_streaming, second_streaming, other_loop
):
    monkeypatch.setattr(invoker_module.roster, "apply_active", lambda inv: (inv, ""))
    monkeypatch.setattr(invoker_module, "inflight", lambda *args: contextlib.nullcontext())
    first = CCInvoker(claude_path="/fixture/claude")
    second = CCInvoker(claude_path="/fixture/claude")
    entered, finish_body, draining, finish_drain = (asyncio.Event() for _ in range(4))

    async def traced(*args):
        entered.set()
        await finish_body.wait()
        return "complete"

    async def drain():
        draining.set()
        await finish_drain.wait()

    for invoker in (first, second):
        monkeypatch.setattr(invoker, "_run_traced", traced)
        monkeypatch.setattr(invoker, "_run_streaming_traced", traced)
    drained = AsyncMock(side_effect=drain)
    monkeypatch.setattr(PeerSegment, "stop_and_drain", drained)
    first_call = first.run_streaming if first_streaming else first.run
    second_call = second.run_streaming if second_streaming else second.run

    async def reject_duplicate():
        with pytest.raises(RuntimeError, match="already active"):
            if other_loop:
                await asyncio.to_thread(lambda: asyncio.run(second_call(peer_invocation)))
            else:
                await second_call(peer_invocation)

    task = asyncio.create_task(first_call(peer_invocation))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await reject_duplicate()
        assert not task.done()
        drained.assert_not_awaited()
        finish_body.set()
        await asyncio.wait_for(draining.wait(), 5)
        await reject_duplicate()
        assert drained.await_count == 1
        finish_drain.set()
        assert await asyncio.wait_for(task, 5) == "complete"
        assert await second_call(peer_invocation) == "complete"
        assert drained.await_count == 2
    finally:
        finish_body.set()
        finish_drain.set()
        await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("cleanup_error", [RuntimeError, asyncio.CancelledError])
async def test_unknown_drain_retains_invocation_claim(peer_invocation, monkeypatch, cleanup_error):
    monkeypatch.setattr(invoker_module.roster, "apply_active", lambda inv: (inv, ""))
    monkeypatch.setattr(invoker_module, "inflight", lambda *args: contextlib.nullcontext())
    invoker = CCInvoker(claude_path="/fixture/claude")
    traced = AsyncMock(return_value="complete")
    monkeypatch.setattr(invoker, "_run_traced", traced)
    drained = AsyncMock(side_effect=cleanup_error())
    monkeypatch.setattr(PeerSegment, "stop_and_drain", drained)
    try:
        with pytest.raises(cleanup_error):
            await invoker.run(peer_invocation)
        with pytest.raises(RuntimeError, match="awaiting reconciliation"):
            await invoker.run(peer_invocation)
        traced.assert_awaited_once()
        drained.assert_awaited_once()
    finally:
        # The fixture deliberately leaves an unknown drain; retire only its
        # synthetic claim so other tests can reuse their fixture identifier.
        with peer_segment._INVOCATION_LOCK:
            peer_segment._INVOCATION_UNITS.discard(peer_invocation.peer_segment.unit_name)


async def test_repeated_cancellation_waits_for_process_drain(peer_invocation, monkeypatch):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    drained = threading.Event()

    def stop_scope(self):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "fixture never released cleanup"
        drained.set()

    monkeypatch.setattr(PeerSegment, "_stop_scope", stop_scope)
    task = asyncio.create_task(peer_invocation.peer_segment.stop_and_drain())
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert drained.is_set()
    finally:
        release.set()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("outcome", ["exception", "error_result"])
@pytest.mark.parametrize("failing_sink", ["none", "writer", "bus"])
async def test_peer_errors_are_redacted_from_child_and_parent_spans(
    peer_invocation, monkeypatch, caplog, streaming, outcome, failing_sink
):
    records = []

    class Writer:
        def record(self, span):
            records.append(span)
            if failing_sink == "writer":
                raise RuntimeError("synthetic writer failure")

    class Bus:
        async def emit(self, *args, **kwargs):
            raise RuntimeError("synthetic bus failure")

    import logging

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        invoker_module, "_runtime_event_bus", lambda: Bus() if failing_sink == "bus" else None
    )

    monkeypatch.setattr(spans, "_writer", Writer())
    monkeypatch.setattr(spans, "_enabled", True)
    monkeypatch.setattr(invoker_module.roster, "apply_active", lambda inv: (inv, ""))
    monkeypatch.setattr(invoker_module, "inflight", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(PeerSegment, "stop_and_drain", AsyncMock())
    invoker = CCInvoker(claude_path="/fixture/claude")
    monkeypatch.setattr(invoker, "_network_preflight", AsyncMock())
    private = "synthetic private error content"
    inner = (
        AsyncMock(side_effect=CCProcessError(private))
        if outcome == "exception"
        else AsyncMock(
            return_value=CCOutput(
                text="",
                session_id="fixture",
                model_used="fixture",
                cost_usd=0,
                input_tokens=0,
                output_tokens=0,
                duration_ms=0,
                exit_code=0,
                is_error=True,
                error_message=private,
            )
        )
    )
    monkeypatch.setattr(invoker, "_run_streaming_inner" if streaming else "_run_inner", inner)
    call = invoker.run_streaming if streaming else invoker.run
    if outcome == "exception":
        with pytest.raises(CCProcessError), spans.start_span("parent"):
            await call(peer_invocation)
    else:
        with spans.start_span("parent"):
            await call(peer_invocation)
    assert len(records) == 2
    assert all(private not in (record.status_message or "") for record in records)
    assert next(r for r in records if r.name == "cc.session").status == "error"
    assert private not in caplog.text


@pytest.mark.parametrize(
    ("load", "active", "empty"),
    [
        ("loaded", "inactive", True),
        ("loaded", "failed", True),
        ("not-found", "inactive", True),
        ("loaded", "active", False),
        ("error", "inactive", False),
        ("not-found", "active", False),
    ],
)
def test_empty_scope_requires_confirmed_terminal_manager_state(
    peer_invocation, tmp_path, monkeypatch, load, active, empty
):
    (tmp_path / "cgroup.controllers").write_text("memory\n")
    monkeypatch.setattr(peer_segment, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(
        peer_segment.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, f"LoadState={load}\nActiveState={active}\nControlGroup=\n", ""
        ),
    )
    if empty:
        peer_invocation.peer_segment._stop_scope()
    else:
        with pytest.raises(RuntimeError, match="control group could not be confirmed"):
            peer_invocation.peer_segment._stop_scope()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel_wait"])
async def test_peer_launch_waits_for_checkout_and_releases_before_drain(
    peer_invocation, monkeypatch, tmp_path, streaming, outcome
):
    import fcntl

    from genesis.cc import checkout_lock

    path = tmp_path / "checkout.lock"
    monkeypatch.setattr(checkout_lock, "checkout_lock_path", lambda: path)
    monkeypatch.setattr(checkout_lock, "_POLL_S", 0.01)
    waiting = asyncio.Event()
    original_admit = invoker_module.admit_launch

    async def admit():
        waiting.set()
        return await original_admit()

    monkeypatch.setattr(invoker_module, "admit_launch", admit)
    reads = []
    monkeypatch.setattr(
        invoker_module.roster, "apply_active", lambda inv: (reads.append(inv) or inv, "")
    )
    invoker = CCInvoker(claude_path="/fixture/claude")

    async def traced(*args):
        probe = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        if outcome == "error":
            raise RuntimeError("fixture launch failure")
        return "complete"

    monkeypatch.setattr(invoker, "_run_streaming_traced" if streaming else "_run_traced", traced)
    drains = []

    async def drain(self):
        probe = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            drains.append(self.unit_name)
        finally:
            os.close(probe)

    monkeypatch.setattr(PeerSegment, "stop_and_drain", drain)
    exclusive = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(exclusive, fcntl.LOCK_EX)
    call = invoker.run_streaming if streaming else invoker.run
    task = asyncio.create_task(call(peer_invocation))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        await asyncio.sleep(0.05)
        assert not reads and not task.done()
        with pytest.raises(RuntimeError, match="already active"):
            await call(peer_invocation)
        assert not drains
        if outcome == "cancel_wait":
            task.cancel()
        fcntl.flock(exclusive, fcntl.LOCK_UN)
        if outcome == "success":
            assert await task == "complete"
        else:
            error = asyncio.CancelledError if outcome == "cancel_wait" else RuntimeError
            with pytest.raises(error):
                await task
        assert len(reads) == (0 if outcome == "cancel_wait" else 1)
        assert drains == [peer_invocation.peer_segment.unit_name]
    finally:
        os.close(exclusive)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
