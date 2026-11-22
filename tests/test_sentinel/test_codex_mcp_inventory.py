"""Exercise paginated protocol reads without connecting to the user's daemon."""

import asyncio
import json
import os
from threading import Event
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from genesis.sentinel import codex_mcp_inventory, codex_mcp_stop
from genesis.sentinel.codex_mcp_inventory import Inventory, _ThreadInventory


@pytest.mark.asyncio
async def test_loaded_pages_and_metadata_only():
    ws = AsyncMock()
    ws.receive_json.side_effect = [
        {"method": "thread/status/changed"},
        {"id": 1, "result": {"data": ["first"], "nextCursor": "next-page"}},
        {"id": 2, "result": {"data": ["second"], "nextCursor": None}},
        {"id": 3, "result": {"thread": {"id": "first", "source": "vscode", "status": {"type": "idle"}}}},
        {"id": 4, "result": {"thread": {"id": "second", "source": {"subAgent": {}}, "status": {"type": "active"}}}},
    ]
    client = _ThreadInventory(ws)
    threads = await client.loaded()
    assert threads == {"first", "second"}
    assert await client.counts(threads) == {"vscode/idle": 1, "subagent/active": 1}
    requests = [call.args[0] for call in ws.send_json.await_args_list]
    assert requests[1]["params"]["cursor"] == "next-page"
    assert all(req["params"]["includeTurns"] is False for req in requests[2:])


@pytest.mark.asyncio
async def test_duplicate_page_cannot_confirm_inventory():
    ws = AsyncMock()
    ws.receive_json.side_effect = [
        {"id": 1, "result": {"data": ["thread"], "nextCursor": "next"}},
        {"id": 2, "result": {"data": ["thread"], "nextCursor": None}},
    ]
    with pytest.raises(ValueError, match="changed"):
        await _ThreadInventory(ws).loaded()


def test_public_summary_omits_thread_and_group_identifiers():
    inventory = Inventory(
        "2026-01-01T12:00:00+00:00", "complete",
        daemon=("boot", 41, 100),
        threads=frozenset({"private-thread"}),
        groups=frozenset({(421, 1234567)}),
    )
    encoded = json.dumps(inventory.summary())
    assert '"threads": 1' in encoded
    assert '"groups": 1' in encoded
    assert "private-thread" not in encoded
    assert "1234567" not in encoded
    assert inventory.summary()["group_scope"] == "stop_eligible_mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unavailable", "absent"])
async def test_sample_records_failed_or_absent_inventory(monkeypatch, status):
    monkeypatch.delenv("GENESIS_CODEX_MCP_INVENTORY_DISABLED", raising=False)
    monkeypatch.setattr(codex_mcp_inventory, "_latest", None)
    monkeypatch.setattr(codex_mcp_stop, "load_mode", lambda: "propose_only")
    monkeypatch.setattr(codex_mcp_inventory, "collect", AsyncMock(return_value=Inventory("fixture", status)))
    bus = AsyncMock()

    await codex_mcp_inventory.sample(bus)

    assert bus.emit.call_args.kwargs["status"] == status
    assert bus.emit.call_args.kwargs["daemon"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("same_daemon", [True, False])
async def test_inventory_reads_only_the_scanned_daemon(tmp_path, monkeypatch, same_daemon):
    path = tmp_path / "fixture.sock"
    pid = os.getpid()
    _boot, _pid, start = codex_mcp_inventory._current_identity(pid)
    scan = codex_mcp_stop.Scan(
        app_servers=[pid],
        app_server_starttimes={pid: start if same_daemon else start + 1},
        groups=[codex_mcp_stop.Group(731, 10, "unknown-label", rss_kb=256, app_server_pid=pid)],
    )
    monkeypatch.setattr(codex_mcp_stop, "scan", lambda: scan)
    monkeypatch.setattr(codex_mcp_inventory, "_control_socket", lambda: path)
    methods = []

    async def on_socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            data = message.json()
            method = data["method"]
            methods.append(method)
            if method == "initialized":
                continue
            result = {
                "initialize": {"userAgent": "codex-test/0.171.0 (private)"},
                "thread/loaded/list": {"data": ["thread-fixture"], "nextCursor": None},
                "thread/read": {"thread": {
                    "id": "thread-fixture", "source": "vscode", "status": {"type": "idle"},
                    "preview": "private payload",
                }},
            }[method]
            await ws.send_json({"id": data["id"], "result": result})
        return ws

    app = web.Application()
    app.router.add_get("/", on_socket)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.UnixSite(runner, str(path)).start()
        result = await codex_mcp_inventory.collect()
    finally:
        await runner.cleanup()
    if same_daemon:
        assert result.status == "complete", result.detail
        assert result.version == "0.171.0"
        assert result.held_kib == 256
        assert result.summary()["thread_counts"] == {"vscode/idle": 1}
        assert "private" not in json.dumps(result.summary())
        assert methods == ["initialize", "initialized", "thread/loaded/list", "thread/read", "thread/loaded/list"]
    else:
        assert result.status == "unavailable"
        assert methods == []


@pytest.mark.asyncio
async def test_socket_close_bounds_the_whole_handshake(tmp_path, monkeypatch):
    closing = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_closes():
        closing.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    ws = AsyncMock()
    ws.close.side_effect = never_closes
    client = MagicMock(ws_connect=AsyncMock(return_value=ws))
    session = MagicMock()
    session.__aenter__.return_value = client
    monkeypatch.setattr(codex_mcp_inventory.aiohttp, "ClientSession", lambda **_: session)
    monkeypatch.setattr(codex_mcp_inventory, "_CLOSE_GRACE_S", 0.01)

    async with asyncio.timeout(1):
        async with codex_mcp_inventory._inventory_socket(tmp_path / "fixture.sock"):
            pass

    assert closing.is_set()
    assert cancelled.is_set()
    session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_does_not_submit_another_process_read():
    release = Event()
    started = Event()

    def blocked_scan():
        started.set()
        return release.wait(3)

    waiting = asyncio.create_task(codex_mcp_inventory._read_process_data(blocked_scan))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        with pytest.raises(ValueError, match="prior process scan"):
            await codex_mcp_inventory._read_process_data(lambda: 1)
    finally:
        release.set()
        await codex_mcp_inventory._process_read
    assert await codex_mcp_inventory._read_process_data(lambda: 2) == 2
