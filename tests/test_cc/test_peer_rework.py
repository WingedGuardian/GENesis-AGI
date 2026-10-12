"""Regression controls for the peer containment replacement."""

import asyncio
import contextlib
import json
import os
import secrets
import subprocess
import sys
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc import invoker as invoker_module
from genesis.cc import peer_segment
from genesis.cc.exceptions import CCProcessError, CCStreamTruncatedError
from genesis.cc.invoker import CCInvoker
from genesis.cc.peer_segment import PeerSegment
from genesis.cc.types import CCOutput
from tests.test_cc.test_peer_segments import _stream_without_result
from tests.test_cc.test_peer_segments import peer_invocation as _original_peer_invocation


@pytest.fixture(name="peer_invocation")
def _peer_fixture(tmp_path):
    return _original_peer_invocation.__wrapped__(tmp_path)


@pytest.mark.parametrize("entry", [".", "", "relative", "absolute", "alias", "nested"])
@pytest.mark.parametrize("command", ["claude", "systemd-run"])
def test_peer_never_executes_cwd_path_entries(peer_invocation, tmp_path, monkeypatch, entry, command):
    cwd = tmp_path / "peer"
    cwd.mkdir()
    nested = cwd / "nested"
    nested.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(cwd, target_is_directory=True)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    for directory, label in ((cwd, "planted"), (nested, "planted"), (trusted, "trusted")):
        for name in ("claude", "systemd-run"):
            path = directory / name
            path.write_text("#!/bin/sh\n/bin/echo " + label + "\n")
            path.chmod(0o700)
    bad = {"absolute": str(cwd), "alias": str(alias), "nested": str(nested)}.get(entry, entry)
    monkeypatch.setenv("PATH", bad + os.pathsep + str(trusted))
    invocation = replace(peer_invocation, working_dir=str(cwd))
    env = CCInvoker._launch_env(CCInvoker(claude_path="/fixture/claude")._build_env(invocation), invocation)
    argv = invocation.peer_segment.launch_args(["claude", "-p"], env, str(cwd), ())
    executable = argv[0] if command == "systemd-run" else argv[argv.index("--") + 1]
    result = subprocess.run([executable], cwd=cwd, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "trusted"
    assert all(os.path.isabs(part) for part in env["PATH"].split(os.pathsep))
    assert str(cwd) not in env["PATH"] and str(alias) not in env["PATH"]


def test_peer_rejects_executable_symlink_into_working_directory(peer_invocation, tmp_path):
    target = tmp_path / "planted"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o700)
    trusted = tmp_path.parent / (tmp_path.name + "-trusted")
    trusted.mkdir()
    (trusted / "claude").symlink_to(target)
    with pytest.raises(CCProcessError):
        peer_invocation.peer_segment.launch_args([str(trusted / "claude")], {"PATH": os.defpath}, str(tmp_path), ())


@pytest.mark.parametrize("path", ["", ".", "relative"])
def test_peer_does_not_search_cwd_when_path_has_no_safe_entries(peer_invocation, path):
    with pytest.raises(CCProcessError):
        peer_invocation.peer_segment.launch_args(["claude"], {"PATH": path}, peer_invocation.working_dir, ())


def test_peer_scope_properties_are_supported_by_systemd_249(peer_invocation):
    args = peer_invocation.peer_segment.scope_args(invoker_module._SCOPE_PROPERTIES)
    properties = {args[i + 1].split("=", 1)[0] for i, value in enumerate(args) if value == "-p"}
    # v249 bus-unit-util.c scope parser plus dbus-scope/kill/cgroup setters.
    assert properties == {"IOWeight", "MemoryHigh", "MemoryMax", "RuntimeMaxSec", "TimeoutStopSec", "KillMode", "SendSIGKILL", "BindsTo", "After"}


@pytest.mark.parametrize("routed", [False, True])
def test_peer_final_environment_keeps_memory_off_and_connectivity(peer_invocation, monkeypatch, routed):
    names = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")
    values = {name: secrets.token_urlsafe(32) for name in names}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    invocation = replace(peer_invocation, anthropic_base_url="https://provider.invalid" if routed else None)
    env = CCInvoker(claude_path="/fixture/claude")._build_env(invocation)
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "0"  # A later merge must not weaken it.
    env["CLAUDE_CODE_DISABLE_ATTACHMENTS"] = "0"
    env = CCInvoker._launch_env(env, invocation)
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env["CLAUDE_CODE_DISABLE_ATTACHMENTS"] == "1"
    assert all(env.get(name) == value for name, value in values.items())


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["stderr", "result", "error_message", "errors"])
async def test_peer_error_prose_never_enters_account_status(peer_invocation, monkeypatch, streaming, failure):
    invoker = CCInvoker(claude_path="/fixture/claude")
    _stream_without_result(invoker, monkeypatch, 1 if failure == "stderr" else 0)
    proc = invoker_module.asyncio.create_subprocess_exec.return_value
    payload = {"type": "result", "is_error": True, "subtype": "error_during_execution", failure: "usage limit reached"}
    encoded = json.dumps(payload).encode()
    proc.communicate = AsyncMock(return_value=(encoded, b"usage limit reached" if failure == "stderr" else b""))
    if streaming:
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(encoded + b"\n")
        proc.stdout.feed_eof()
        if failure == "stderr":
            proc.stderr = asyncio.StreamReader()
            proc.stderr.feed_data(b"usage limit reached")
            proc.stderr.feed_eof()
    classifier = MagicMock(return_value=CCProcessError("fixture classification"))
    notify = AsyncMock()
    monkeypatch.setattr(invoker, "_classify_error", classifier)
    monkeypatch.setattr(invoker, "_notify_status_change", notify)
    with pytest.raises(CCProcessError) as error:
        await (invoker.run_streaming(peer_invocation) if streaming else invoker.run(peer_invocation))
    assert type(error.value) is CCProcessError
    assert "usage limit reached" not in str(error.value)
    classifier.assert_not_called()
    notify.assert_not_awaited()


@pytest.mark.parametrize("body", ["success", "error", "cancel"])
@pytest.mark.parametrize("drain", ["success", "error", "cancel"])
@pytest.mark.parametrize("cancel_count", [0, 1, 3])
async def test_peer_cleanup_distinguishes_caller_cancel_from_unknown_drain(peer_invocation, monkeypatch, body, drain, cancel_count):
    entered, release = asyncio.Event(), asyncio.Event()
    segment = peer_invocation.peer_segment
    async def cleanup(self):
        entered.set()
        await release.wait()
        if drain == "error":
            raise LookupError("fixture cleanup failed")
        if drain == "cancel":
            raise asyncio.CancelledError
    monkeypatch.setattr(PeerSegment, "stop_and_drain", cleanup)
    async def call():
        async with segment.invocation():
            if body == "error":
                raise ValueError("fixture body failed")
            if body == "cancel":
                raise asyncio.CancelledError
    task = asyncio.create_task(call())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        expected = LookupError if drain == "error" else asyncio.CancelledError if drain == "cancel" or cancel_count or body == "cancel" else ValueError if body == "error" else None
        if expected:
            with pytest.raises(expected):
                await task
        else:
            await task
        assert (segment.unit_name in peer_segment._INVOCATION_UNITS) == (drain != "success")
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        peer_segment._INVOCATION_UNITS.discard(segment.unit_name)


async def test_peer_empty_output_callback_exception_keeps_private_text_out_of_logs(peer_invocation, caplog):
    invoker = CCInvoker(claude_path="/fixture/claude")
    canary = secrets.token_hex(32)
    invocation = replace(peer_invocation, prompt=canary, expect_output=True)
    async def callback(invocation, output):
        raise RuntimeError(invocation.prompt)
    invoker._on_cc_empty_output = callback
    output = CCOutput(text="", session_id="fixture", model_used="fixture", cost_usd=0, input_tokens=0, output_tokens=0, duration_ms=0, exit_code=0)
    await invoker._fire_empty_output_callback(invocation, output)
    assert "CC empty-output callback failed" in caplog.text
    assert canary not in caplog.text


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("routed", [False, True])
async def test_both_spawns_receive_final_peer_policy_after_login_routing(peer_invocation, monkeypatch, streaming, routed):
    launcher = PeerSegment.launch_args
    invoker = CCInvoker(claude_path=sys.executable)
    _stream_without_result(invoker, monkeypatch, 0)
    monkeypatch.setattr(PeerSegment, "launch_args", launcher)
    monkeypatch.setattr(invoker, "_build_env", CCInvoker._build_env.__get__(invoker))
    monkeypatch.setattr(invoker, "_launch_env", CCInvoker._launch_env)
    monkeypatch.setattr(invoker, "_apply_login_fallback", CCInvoker._apply_login_fallback.__get__(invoker))
    fallback = AsyncMock(return_value={"CLAUDE_CODE_OAUTH_TOKEN": secrets.token_urlsafe(32)})
    monkeypatch.setattr("genesis.cc.login_health.fallback_env_if_login_dead", fallback)
    monkeypatch.setenv("PATH", peer_invocation.working_dir + os.pathsep + os.defpath)
    proxy = "http://" + secrets.token_hex(8) + ".invalid"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    invocation = replace(peer_invocation, anthropic_base_url="https://provider.invalid" if routed else None)
    proc = invoker_module.asyncio.create_subprocess_exec.return_value
    proc.communicate = AsyncMock(return_value=(json.dumps({"type": "result", "result": "complete", "is_error": False}).encode(), b""))
    await (invoker.run_streaming(invocation) if streaming else invoker.run(invocation))
    call = invoker_module.asyncio.create_subprocess_exec.call_args
    env = call.kwargs["env"]
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and env["HTTPS_PROXY"] == proxy
    assert env["CLAUDE_CODE_DISABLE_ATTACHMENTS"] == "1"
    assert peer_invocation.working_dir not in env["PATH"].split(os.pathsep)
    assert os.path.isabs(call.args[0]) and call.args[call.args.index("--") + 1] == os.path.realpath(sys.executable)
    assert fallback.await_count == (0 if routed else 1)


async def test_peer_error_after_dropped_line_remains_unreplayable(peer_invocation, monkeypatch):
    invoker = CCInvoker(claude_path="/fixture/claude")
    _stream_without_result(invoker, monkeypatch, 0)
    proc = invoker_module.asyncio.create_subprocess_exec.return_value
    proc.stdout = asyncio.StreamReader(limit=256)
    proc.stdout.feed_data(b"x" * 300 + b"\n" + json.dumps({"type": "result", "is_error": True, "result": "usage limit reached"}).encode() + b"\n")
    proc.stdout.feed_eof()
    notify = AsyncMock()
    monkeypatch.setattr(invoker, "_notify_status_change", notify)
    with pytest.raises(CCStreamTruncatedError) as error:
        await invoker.run_streaming(peer_invocation)
    assert type(error.value.__cause__) is CCProcessError
    notify.assert_not_awaited()
