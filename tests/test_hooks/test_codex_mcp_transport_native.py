"""Opt-in synthetic MCP qualification against the installed native Codex binary."""

import hashlib
import json
import os
import selectors
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from tests.test_hooks.native_codex import AppServer, configure, fixture_provider

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
    reason="explicit installed-client qualification only",
)
FIXTURE = Path(__file__).with_name("mcp_transport_fixture.py")


class Service:
    def __init__(self, root, env, stateful, fixture_path=FIXTURE):
        self.root, self.env, self.stateful = root, env, stateful
        self.fixture_path = fixture_path
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}/mcp"
        self.process = None
        self.log = (root / "service-stderr.txt").open("a")

    def start(self, generation=1, failure_mode="normal"):
        assert self.process is None
        argv = [sys.executable, str(self.fixture_path), "--fd", str(self.socket.fileno()),
                "--generation", str(generation), "--failure-mode", failure_mode]
        if self.stateful:
            argv.append("--stateful")
        self.process = subprocess.Popen(
            argv, env=self.env, cwd=self.root, pass_fds=(self.socket.fileno(),),
            stdout=subprocess.PIPE, stderr=self.log, text=True,
        )
        # Readiness deadline bounds a failed fixture import/start, not real work.
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            assert selector.select(30), "fixture did not signal readiness"
            assert self.process.stdout.readline().strip() == "READY"

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process.stdout.close()
            self.process = None

    def close(self):
        self.stop()
        self.socket.close()
        self.log.close()


def rpc(driver, method, params, request_id):
    """Retain expected protocol errors as observations, without hiding crashes."""
    driver.send(method, params, request_id)
    deadline = time.monotonic() + 30
    while True:
        event = driver.pending.get(timeout=max(0, deadline - time.monotonic()))
        driver.events.append(event)
        assert not ("closed" in event or "invalid_json" in event), event
        assert not ("method" in event and "id" in event), event
        if event.get("id") == request_id:
            return event
        assert "id" not in event and "error" not in event, event


@pytest.mark.parametrize("stateful", [False, True], ids=["stateless", "stateful"])
def test_same_thread_service_restart(tmp_path, stateful):
    binary = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN")
    assert binary and Path(binary).is_absolute() and Path(binary).is_file(), "explicit native binary required"
    root = tmp_path / "native transport ü"
    root.mkdir()
    evidence = {"stateful": stateful, "source_hashes": {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__), FIXTURE, Path(__file__).with_name("native_codex.py"))
    }}
    with fixture_provider({}) as (provider_url, requests):
        env = configure(root, provider_url, "allow")
        env["FIXTURE_TOKEN"] = "fixture-local-only"
        service = Service(root, env, stateful)
        cleanup = ExitStack()
        cleanup.callback(service.close)
        driver = None
        try:
            service.start()
            config = root / "codex/config.toml"
            text = config.read_text().split("[[hooks.PreToolUse]]")[0]
            config.write_text(text + f'''
[mcp_servers.fixture]
url = {json.dumps(service.url)}
bearer_token_env_var = "FIXTURE_TOKEN"
http_headers = {{"x-fixture-caller" = "caller-a"}}
startup_timeout_sec = 10
tool_timeout_sec = 5
default_tools_approval_mode = "approve"
''')
            driver = AppServer(binary, root, env)
            cleanup.callback(driver.close)
            evidence["initialize"] = driver.initialize()
            thread = driver.request("thread/start", {
                "cwd": str(root / "workspace"), "ephemeral": True,
            }, 2)["thread"]["id"]
            params = {"threadId": thread, "server": "fixture", "tool": "echo",
                      "arguments": {"value": "healthy"}}
            first = rpc(driver, "mcpServer/tool/call", params, 3)
            evidence["healthy"] = first
            assert "result" in first and not first["result"].get("isError"), first
            assert json.loads(first["result"]["content"][0]["text"])["caller"] == "caller-a"
            service.stop()
            service.start()
            evidence["restart_same_catalog"] = rpc(driver, "mcpServer/tool/call", params, 4)
            same = evidence["restart_same_catalog"]
            assert "result" in same and not same["result"].get("isError"), same
            service.stop()
            service.start(2)
            evidence["restart_changed_catalog"] = rpc(driver, "mcpServer/tool/call", params, 5)
            changed = evidence["restart_changed_catalog"]
            assert "result" in changed and not changed["result"].get("isError"), changed
            evidence["catalog_before_reload"] = rpc(driver, "mcpServerStatus/list", {"threadId": thread}, 6)
            evidence["catalog_before_reload_scoped"] = rpc(driver, "mcpServerStatus/list", {
                "threadId": thread, "serverName": "fixture",
            }, 11)
            evidence["reload"] = rpc(driver, "config/mcpServer/reload", {}, 7)
            assert "result" in evidence["reload"] and "error" not in evidence["reload"], evidence["reload"]
            evidence["after_reload"] = rpc(driver, "mcpServer/tool/call", params, 8)
            evidence["catalog_after_reload"] = rpc(driver, "mcpServerStatus/list", {
                "threadId": thread, "serverName": "fixture",
            }, 9)
            evidence["new_tool"] = rpc(driver, "mcpServer/tool/call", {**params, "tool": "new_tool", "arguments": {}}, 10)
            # Distinguish an asynchronous reload from a persistently stale
            # thread connection; the independent/fresh-thread views are controls.
            time.sleep(5)
            evidence["catalog_after_grace"] = rpc(driver, "mcpServerStatus/list", {
                "threadId": thread, "serverName": "fixture",
            }, 12)
            fresh = driver.request("thread/start", {
                "cwd": str(root / "workspace"), "ephemeral": True,
            }, 13)["thread"]["id"]
            evidence["fresh_thread_catalog"] = rpc(driver, "mcpServerStatus/list", {
                "threadId": fresh, "serverName": "fixture",
            }, 14)
            catalog = evidence["catalog_after_grace"]["result"]
            assert catalog.get("nextCursor") is None and len(catalog["data"]) == 1, catalog
            server = catalog["data"][0]
            assert server["name"] == "fixture" and server["runtimeStatus"] == "connected", server
            assert server["toolsError"] is None, server
            # Owner-approved parity permits the starting catalog; a future
            # client that refreshes it also satisfies the recovery contract.
            original = {"echo", "commit", "read_count"}
            assert set(server["tools"]) in (original, original | {"new_tool"}), server
            fresh_catalog = evidence["fresh_thread_catalog"]["result"]
            assert fresh_catalog.get("nextCursor") is None and len(fresh_catalog["data"]) == 1, fresh_catalog
            fresh_server = fresh_catalog["data"][0]
            assert fresh_server["runtimeStatus"] == "connected" and fresh_server["toolsError"] is None, fresh_server
            assert set(fresh_server["tools"]) == original | {"new_tool"}, fresh_server
            assert "result" in evidence["after_reload"] and not evidence["after_reload"]["result"].get("isError"), evidence
            assert "result" in evidence["new_tool"] and not evidence["new_tool"]["result"].get("isError"), evidence
            assert json.loads(evidence["after_reload"]["result"]["content"][0]["text"])["generation"] == 2
            assert evidence["new_tool"]["result"]["content"][0]["text"] == "new catalog"
            service.stop()
            service.start(3)  # Removes commit and renames read_count to count_writes.
            evidence["obsolete_tools"] = {}
            for index, name in enumerate(("commit", "read_count")):
                began = time.monotonic()
                reply = rpc(driver, "mcpServer/tool/call", {
                    **params, "tool": name,
                    "arguments": {"value": "must-not-write"} if name == "commit" else {},
                }, 15 + index)
                evidence["obsolete_tools"][name] = {"reply": reply, "elapsed_seconds": time.monotonic() - began}
                # A transport error or timeout is not a clear missing-tool error.
                assert reply.get("result", {}).get("isError") is True, reply
                message = " ".join(block.get("text", "") for block in reply["result"]["content"])
                assert name in message and "Unknown tool" in message, reply
            assert not (root / "writes.jsonl").exists(), "removed commit must not execute"
            evidence["healthy_after_removal"] = driver.request("mcpServer/tool/call", params, 17)
            assert not evidence["healthy_after_removal"].get("isError"), evidence
            assert json.loads(evidence["healthy_after_removal"]["content"][0]["text"])["generation"] == 3
            newest = driver.request("thread/start", {
                "cwd": str(root / "workspace"), "ephemeral": True,
            }, 18)["thread"]["id"]
            latest = driver.request("mcpServerStatus/list", {
                "threadId": newest, "serverName": "fixture",
            }, 19)
            evidence["fresh_after_removal"] = latest
            assert latest.get("nextCursor") is None and len(latest["data"]) == 1, latest
            assert set(latest["data"][0]["tools"]) == {"echo", "count_writes", "new_tool"}, latest
            renamed = driver.request("mcpServer/tool/call", {
                "threadId": newest, "server": "fixture", "tool": "count_writes", "arguments": {},
            }, 20)
            assert not renamed.get("isError") and json.loads(renamed["content"][0]["text"]) == 0, renamed
            evidence["renamed_tool"] = renamed
        finally:
            try:
                cleanup.close()
            finally:
                evidence["model_requests"] = len(requests)
                # stdout survives install sandbox disposal even if file output fails.
                print("MCP_TRANSPORT_EVIDENCE " + json.dumps(evidence, sort_keys=True), flush=True)
                (root / "transport-evidence.json").write_text(json.dumps(evidence, indent=2))
        assert not requests, "MCP qualification must not invoke a model"
