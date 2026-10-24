"""Actual Unix connections around the real adapter with a fake SDK child."""

import asyncio
import contextlib
import json
import os
import socket
import stat

import pytest
from claude_agent_sdk import ToolPermissionContext

from tests.test_live_agent.test_agent import (
    Harness,
    owner_environment,  # noqa: F401
    result,
)


@pytest.fixture
async def running(tmp_path):
    from genesis.live_agent.host import LocalHost

    h = Harness(tmp_path)
    host = LocalHost(h.agent, tmp_path / "runtime" / "control.sock")
    stop = asyncio.Event()
    task = asyncio.create_task(host.run(stop))
    waiter = asyncio.create_task(host.ready.wait())
    await asyncio.wait([task, waiter], return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        waiter.cancel()
        await task
    try:
        yield h, host
    finally:
        stop.set()
        await task


async def connect(host):
    reader, writer = await asyncio.open_unix_connection(str(host.socket_path))
    snapshot = json.loads(await reader.readline())
    assert snapshot["type"] == "snapshot"
    return reader, writer, snapshot


async def request(reader, writer, **command):
    writer.write(json.dumps({"id": "req", **command}).encode() + b"\n")
    await writer.drain()
    while True:
        msg = json.loads(await reader.readline())
        if msg.get("type") == "response" and msg.get("id") == "req":
            return msg


async def test_owner_turn_events_and_reattach_preserve_session(running):
    h, host = running
    reader, writer, snapshot = await connect(host)
    assert snapshot["history"] == "not replayed"
    assert stat.S_IMODE(host.socket_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(host.socket_path.parent.stat().st_mode) == 0o700
    assert (await request(reader, writer, op="turn", text="read-only task"))["ok"]
    assert len(h.client.sent) == 1
    await h.feed(result(result="task finished"))
    while True:
        event = json.loads(await reader.readline())
        if event.get("kind") == "finished":
            assert event["data"]["result"] == "task finished"
            break
    writer.close()
    await writer.wait_closed()
    while host.controller_connected:
        await asyncio.sleep(0)
    assert h.agent.status()["connected"]
    reader, writer, _ = await connect(host)
    assert (await request(reader, writer, op="status"))["ok"]
    writer.close()
    await writer.wait_closed()


async def test_full_pending_snapshot_and_explicit_boolean_approval(running):
    h, host = running
    approval = asyncio.create_task(h.agent._permission(
        "Write", {"file_path": "synthetic", "content": "complete contents"},
        ToolPermissionContext(decision_reason="outside directory"),
    ))
    while not h.agent.status()["pending_approvals"]:
        await asyncio.sleep(0)
    reader, writer, snapshot = await connect(host)
    ask = snapshot["status"]["pending_approvals"][0]
    assert ask["input"]["content"] == "complete contents"
    assert not (await request(reader, writer, op="answer", approval_id=ask["id"], allow="false"))["ok"]
    assert not approval.done()
    assert not (await request(reader, writer, op="turn", text="yes"))["ok"]
    assert (await request(reader, writer, op="answer", approval_id=ask["id"], allow=False))["ok"]
    assert "did not approve" in (await approval).message
    writer.close()
    await writer.wait_closed()


@pytest.mark.parametrize("command", [
    {"op": "turn", "text": "x", "initiator": "model_delegation"},
    {"op": "turn", "text": ""}, {"op": "unknown"}, {"op": "answer", "allow": True},
])
async def test_invalid_commands_never_send(running, command):
    h, host = running
    reader, writer, _ = await connect(host)
    assert not (await request(reader, writer, **command))["ok"]
    assert h.client.sent == []
    writer.close()
    await writer.wait_closed()


async def test_second_controller_refused_and_unknown_peer_denied(running, monkeypatch):
    from genesis.live_agent import host as module

    h, host = running
    reader, writer, _ = await connect(host)
    other, second = await asyncio.open_unix_connection(str(host.socket_path))
    assert not json.loads(await other.readline())["ok"]
    assert await other.readline() == b""
    second.close()
    await second.wait_closed()
    writer.close()
    await writer.wait_closed()
    while host.controller_connected:
        await asyncio.sleep(0)
    monkeypatch.setattr(module, "_peer_uid", lambda writer: os.getuid() + 1)
    other, second = await asyncio.open_unix_connection(str(host.socket_path))
    assert await other.readline() == b""
    assert h.client.sent == []
    second.close()
    await second.wait_closed()


async def test_second_host_cannot_displace_live_endpoint(running, tmp_path):
    from genesis.live_agent.host import LocalHost

    h, host = running
    other = Harness(tmp_path / "different-state")
    duplicate = LocalHost(other.agent, host.socket_path)
    with pytest.raises(RuntimeError, match="already owned"):
        await duplicate.run(asyncio.Event())
    assert other.clients == []
    reader, writer, _ = await connect(host)
    assert (await request(reader, writer, op="status"))["ok"]
    writer.close()
    await writer.wait_closed()


@pytest.mark.parametrize("kind", ["file", "symlink", "public-directory"])
async def test_refuses_unsafe_runtime_paths(tmp_path, kind):
    from genesis.live_agent.host import LocalHost

    folder = tmp_path / "runtime"
    folder.mkdir(mode=0o700)
    path = folder / "control.sock"
    if kind == "file":
        path.write_text("keep me")
    elif kind == "symlink":
        path.symlink_to(tmp_path / "absent")
    else:
        folder.chmod(0o777)
    h = Harness(tmp_path)
    with pytest.raises((PermissionError, FileExistsError)):
        await LocalHost(h.agent, path).run(asyncio.Event())
    assert h.clients == []
    if kind == "file":
        assert path.read_text() == "keep me"


async def test_stale_socket_recovers_and_shutdown_removes_only_own_inode(tmp_path):
    from genesis.live_agent.host import LocalHost

    folder = tmp_path / "runtime"
    folder.mkdir(mode=0o700)
    path = folder / "control.sock"
    with socket.socket(socket.AF_UNIX) as stale:
        stale.bind(str(path))
    h = Harness(tmp_path)
    host = LocalHost(h.agent, path)
    stop = asyncio.Event()
    task = asyncio.create_task(host.run(stop))
    await host.ready.wait()
    path.unlink()
    path.write_text("replacement must survive cleanup")
    stop.set()
    await task
    assert not h.agent.status()["connected"]
    assert path.read_text() == "replacement must survive cleanup"


async def test_unexpected_sdk_eof_tears_down_host(running):
    h, host = running
    reader, writer, _ = await connect(host)
    await h.feed(None)
    while host.socket_path.exists():
        await asyncio.sleep(0)
    assert not h.agent.status()["connected"]
    writer.close()
    with contextlib.suppress(ConnectionError):
        await writer.wait_closed()


async def test_redirect_denies_pending_and_late_approval_is_refused(running):
    h, host = running
    reader, writer, _ = await connect(host)
    pending = asyncio.create_task(h.agent._permission("Write", {"file_path": "synthetic"}, ToolPermissionContext()))
    while not h.agent.status()["pending_approvals"]:
        await asyncio.sleep(0)
    ask_id = h.agent.status()["pending_approvals"][0]["id"]
    assert (await request(reader, writer, op="steer", text="change course"))["ok"]
    assert "did not approve" in (await pending).message
    assert not (await request(reader, writer, op="answer", approval_id=ask_id, allow=True))["ok"]
    assert len(h.client.sent) == 1
    writer.close()
    await writer.wait_closed()


async def test_slow_controller_is_disconnected_without_stopping_agent(running, monkeypatch):
    h, host = running
    reader, writer, _ = await connect(host)

    async def blocked():
        await asyncio.Event().wait()

    monkeypatch.setattr(host._controller.writer, "drain", blocked)
    for n in range(150):
        h.agent._emit("progress", f"event {n}")
    while host.controller_connected:
        await asyncio.sleep(0)
    assert h.agent.status()["connected"]
    reader2, writer2, snapshot = await connect(host)
    assert snapshot["history"] == "not replayed"
    assert (await request(reader2, writer2, op="status"))["ok"]
    writer.close()
    writer2.close()
    await asyncio.gather(writer.wait_closed(), writer2.wait_closed(), return_exceptions=True)


async def test_malformed_and_large_commands_remain_local(running):
    h, host = running
    reader, writer, _ = await connect(host)
    for raw in (b"not-json\n", b"[]\n", b'{"op":"turn","text":"x"}\n'):
        writer.write(raw)
        await writer.drain()
        assert not json.loads(await reader.readline())["ok"]
    text = "orchid-\u2603" * 16000
    assert (await request(reader, writer, op="turn", text=text))["ok"]
    assert h.client.sent[-1].endswith(text)
    writer.close()
    await writer.wait_closed()


async def test_accepted_writer_closed_even_if_handler_never_starts(tmp_path):
    from types import SimpleNamespace

    from genesis.live_agent.host import LocalHost

    aborted = []
    writer = SimpleNamespace(transport=SimpleNamespace(abort=lambda: aborted.append(True)))
    host = LocalHost(Harness(tmp_path).agent, tmp_path / "control.sock")
    host._accept(None, writer)
    tasks = list(host._handlers)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert aborted


def test_live_listener_with_full_backlog_is_refused_without_blocking(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    # Keep the old blocking implementation out of pytest's event loop. A
    # subprocess timeout kills only this scratch listener, never a real unit.
    code = """
import socket, sys
from pathlib import Path
from genesis.live_agent.host import LocalHost
path = Path(sys.argv[1])
with socket.socket(socket.AF_UNIX) as listener, socket.socket(socket.AF_UNIX) as first:
    listener.bind(str(path))
    listener.listen(0)
    first.connect(str(path))
    try:
        LocalHost(None, path)._remove_stale_socket()
    except BlockingIOError:
        print('REFUSED')
    else:
        raise AssertionError('live listener displaced')
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "full.sock")],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "REFUSED"
