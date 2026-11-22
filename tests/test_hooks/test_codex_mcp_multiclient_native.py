"""Opt-in caller separation and subscription lifecycle using two native clients."""

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import pytest

from scripts.mcp_process_report import memory
from tests.test_hooks.native_codex import configure, fixture_provider
from tests.test_hooks.native_mcp_connections import ConnectedServer, Connection
from tests.test_hooks.test_codex_mcp_transport_native import Service, rpc

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1", reason="installed-client qualification only",
)


def loaded(client, first_id):
    """Inventory every page; repeated cursors are failure, never completeness."""
    result, cursor, seen = [], None, set()
    while True:
        page = client.request("thread/loaded/list", {"limit": 2, "cursor": cursor}, first_id + len(seen))
        result.extend(page["data"])
        cursor = page.get("nextCursor")
        if cursor is None:
            return result
        assert cursor not in seen, "loaded thread inventory pagination repeated a cursor"
        seen.add(cursor)


def test_inventory_preserves_the_daemon_client_identity(tmp_path, monkeypatch):
    from genesis.sentinel import codex_mcp_inventory, codex_mcp_stop

    binary = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN")
    assert binary and Path(binary).is_absolute() and Path(binary).is_file()
    with fixture_provider({}) as (url, requests), ExitStack() as cleanup:
        server = ConnectedServer(binary, tmp_path, configure(tmp_path, url, "allow"))
        cleanup.callback(server.close)
        server.connect()  # Establish an ordinary originating client identity.

        def user_agent():
            observer = Connection(server.socket_path)
            try:
                result = observer.request(
                    "initialize", {"clientInfo": {"name": "codex-backend", "version": "observer"}}, 1,
                )
                return result["userAgent"]
            finally:
                observer.close()

        before = user_agent()
        assert "genesis_native_fixture" in before
        pid = server.process.pid
        _, _, start = codex_mcp_inventory._current_identity(pid)
        scan = codex_mcp_stop.Scan(app_servers=[pid], app_server_starttimes={pid: start})
        monkeypatch.setattr(codex_mcp_stop, "scan", lambda: scan)
        monkeypatch.setattr(codex_mcp_inventory, "_control_socket", lambda: server.socket_path)

        inventory = asyncio.run(codex_mcp_inventory.collect())

        assert inventory.status == "complete", inventory.detail
        assert user_agent() == before
        assert not requests


@pytest.mark.parametrize("stateful", [False, True], ids=["stateless", "stateful"])
def test_two_clients_auth_isolation_and_ten_unsubscribe_cycles(tmp_path, stateful):
    binary = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN")
    assert binary and Path(binary).is_absolute() and Path(binary).is_file()
    root = tmp_path / "two clients ü"
    root.mkdir()
    evidence = {"stateful": stateful}
    with fixture_provider({}) as (url, requests):
        env = configure(root, url, "allow")
        env.update(FIXTURE_TOKEN="fixture-local-only", BAD_FIXTURE_TOKEN="wrong-fixture-token")
        service = Service(root, env, stateful=stateful)
        cleanup = ExitStack()
        cleanup.callback(service.close)
        server = None
        try:
            service.start()
            config = root / "codex/config.toml"
            config.write_text(config.read_text().split("[[hooks.PreToolUse]]")[0] + f'''
[mcp_servers.fixture]
url = {json.dumps(service.url)}
bearer_token_env_var = "FIXTURE_TOKEN"
startup_timeout_sec = 10
tool_timeout_sec = 5
default_tools_approval_mode = "approve"
''')
            server = ConnectedServer(binary, root, env)
            cleanup.callback(server.close)
            clients = [server.connect(), server.connect()]
            threads = [client.request("thread/start", {
                "cwd": str(root / "workspace"), "ephemeral": True,
                "config": {"mcp_servers.fixture.http_headers": {"x-fixture-caller": caller}},
            }, 2)["thread"]["id"] for client, caller in zip(clients, ("a", "b"), strict=True)]

            def echo(index):
                return rpc(clients[index], "mcpServer/tool/call", {
                    "threadId": threads[index], "server": "fixture", "tool": "echo",
                    "arguments": {"value": f"parallel-{index}"},
                }, 3)

            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(echo, range(2)))
            evidence["parallel"] = replies
            assert [json.loads(r["result"]["content"][0]["text"])["caller"] for r in replies] == ["a", "b"]
            assert [json.loads(r["result"]["content"][0]["text"])["value"] for r in replies] == ["parallel-0", "parallel-1"]
            for generation in (1, 2):
                service.stop()
                service.start(generation)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    recovered = list(pool.map(echo, range(2)))
                evidence[f"restart_generation_{generation}"] = recovered
                assert all("result" in reply and not reply["result"].get("isError") for reply in recovered), recovered
                values = [json.loads(reply["result"]["content"][0]["text"]) for reply in recovered]
                assert [value["caller"] for value in values] == ["a", "b"], values
                assert [value["value"] for value in values] == ["parallel-0", "parallel-1"], values
                assert all(value["generation"] == generation for value in values), values
            client = clients[0]
            bad = client.request("thread/start", {
                "cwd": str(root / "workspace"), "ephemeral": True,
                "config": {"mcp_servers.fixture.bearer_token_env_var": "BAD_FIXTURE_TOKEN"},
            }, 4)["thread"]["id"]
            evidence["bad_auth"] = rpc(client, "mcpServer/tool/call", {
                "threadId": bad, "server": "fixture", "tool": "echo", "arguments": {"value": "denied"},
            }, 5)
            assert "error" in evidence["bad_auth"] or evidence["bad_auth"].get("result", {}).get("isError"), evidence
            evidence["bad_status"] = client.request("mcpServerStatus/list", {"threadId": bad}, 6)
            assert "401" in json.dumps(evidence["bad_status"]), evidence["bad_status"]
            evidence["good_after_bad"] = echo(1)
            assert "result" in evidence["good_after_bad"] and not evidence["good_after_bad"]["result"].get("isError")
            evidence["loaded_before"] = loaded(client, 100)
            evidence["memory_before"] = memory(Path("/proc") / str(server.process.pid))
            evidence["cycles"] = []
            for cycle in range(10):
                request_id = 200 + cycle * 3
                thread = client.request("thread/start", {
                    "cwd": str(root / "workspace"), "ephemeral": True,
                }, request_id)["thread"]["id"]
                result = client.request("mcpServer/tool/call", {
                    "threadId": thread, "server": "fixture", "tool": "echo", "arguments": {"value": str(cycle)},
                }, request_id + 1)
                assert not result.get("isError"), result
                unsubscribed = client.request("thread/unsubscribe", {"threadId": thread}, request_id + 2)
                assert unsubscribed["status"] == "unsubscribed", unsubscribed
                evidence["cycles"].append({"thread": thread, "unsubscribe": unsubscribed})
            # A short observation window, NOT a claim that documented idle
            # retention has expired or that retained threads are leaked.
            time.sleep(5)
            evidence["observation_grace_seconds"] = 5
            evidence["loaded_after"] = loaded(client, 300)
            evidence["memory_after"] = memory(Path("/proc") / str(server.process.pid))
        finally:
            try:
                cleanup.close()
            finally:
                evidence["model_requests"] = len(requests)
                print("MCP_MULTICLIENT_EVIDENCE " + json.dumps(evidence, sort_keys=True), flush=True)
        assert not requests, "MCP-only qualification must not invoke a model"
