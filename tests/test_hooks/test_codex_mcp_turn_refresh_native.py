"""Separately opted-in, one mocked turn tests both HTTP catalogs after reload."""

import hashlib
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from tests.test_hooks.native_codex import AppServer, configure, fixture_provider
from tests.test_hooks.test_codex_mcp_transport_native import FIXTURE, Service

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_MOCK_TURN_TESTS") != "1",
    reason="one mocked turn requires separate explicit qualification opt-in",
)


def test_one_mocked_turn_refreshes_both_existing_thread_catalogs(tmp_path):
    run_refresh_probe(tmp_path)


def run_refresh_probe(tmp_path, *, notification_fixture=None):
    binary = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN")
    assert binary and Path(binary).is_absolute() and Path(binary).is_file()
    root = tmp_path / "mocked turn"
    root.mkdir()
    modes = ("stateful", "stateless")
    item = {
        "type": "custom_tool_call", "id": "ct_catalog", "call_id": "call_catalog",
        "namespace": "functions", "name": "exec",
        "input": (
            'const catalog = ALL_TOOLS.filter(t => t.name.startsWith("mcp__stateful__") '
            '|| t.name.startsWith("mcp__stateless__")).map(t => t.name).sort(); '
            'const results = {}; for (const mode of ["stateful", "stateless"]) { '
            'try { results[mode] = await tools["mcp__" + mode + "__new_tool"]({}); } '
            'catch (error) { results[mode] = {error: String(error)}; } } '
            'text("CATALOG_TURN:" + JSON.stringify({catalog, results}));'
        ),
    }
    evidence = {"turn_submissions": 0, "procedure": "list_changed" if notification_fixture else "reload",
                "source_hashes": {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            Path(__file__), FIXTURE, Path(__file__).with_name("native_codex.py"),
            Path(__file__).with_name("test_codex_mcp_transport_native.py"),
            *([notification_fixture, Path(__file__).with_name("test_codex_mcp_notifications_native.py")]
              if notification_fixture else []),
        )
    }}
    with fixture_provider(item) as (url, requests):
        env = configure(root, url, "allow")
        env["FIXTURE_TOKEN"] = "fixture-local-only"
        cleanup = ExitStack()
        try:
            services = {}
            config = root / "codex/config.toml"
            text = config.read_text().split("[[hooks.PreToolUse]]")[0]
            for mode in modes:
                directory = root / mode
                directory.mkdir()
                service = Service(directory, env, stateful=mode == "stateful",
                                  fixture_path=notification_fixture or FIXTURE)
                cleanup.callback(service.close)
                service.start()
                services[mode] = service
                text += f'''
[mcp_servers.{mode}]
url = {json.dumps(service.url)}
bearer_token_env_var = "FIXTURE_TOKEN"
required = true
startup_timeout_sec = 10
tool_timeout_sec = 5
default_tools_approval_mode = "approve"
'''
            config.write_text(text)
            driver = AppServer(binary, root, env)
            cleanup.callback(driver.close)
            evidence["initialize"] = driver.initialize()
            effective = driver.request("config/read", {"includeLayers": False}, 20)["config"]
            assert effective["model_provider"] == "fixture", "effective provider must remain local"
            assert effective["model_providers"]["fixture"]["base_url"] == url
            assert effective["model_providers"]["fixture"]["requires_openai_auth"] is False
            started = driver.request("thread/start", {
                "cwd": str(root / "workspace"), "ephemeral": True,
                "modelProvider": "fixture", "allowProviderModelFallback": False,
            }, 2)
            assert started["modelProvider"] == "fixture"
            thread = started["thread"]["id"]

            def catalogs(first_id):
                result = {}
                for index, mode in enumerate(modes):
                    page = driver.request("mcpServerStatus/list", {
                        "threadId": thread, "serverName": mode,
                    }, first_id + index)
                    assert page.get("nextCursor") is None and len(page["data"]) == 1, page
                    server = page["data"][0]
                    assert server["name"] == mode and server["runtimeStatus"] == "connected", server
                    assert server["toolsError"] is None, server
                    result[mode] = sorted(server["tools"])
                return result

            evidence["initial"] = catalogs(3)
            initial_names = ["audit", "change_catalog", "echo"] if notification_fixture else ["commit", "echo", "read_count"]
            assert all(names == initial_names for names in evidence["initial"].values())

            def audit(first_id):
                result = {}
                for index, mode in enumerate(modes):
                    reply = driver.request("mcpServer/tool/call", {
                        "threadId": thread, "server": mode, "tool": "audit", "arguments": {},
                    }, first_id + index)
                    assert not reply.get("isError"), reply
                    result[mode] = json.loads(reply["content"][0]["text"])
                return result

            if notification_fixture:
                evidence["wire_before"] = audit(30)
                for index, mode in enumerate(modes):
                    changed = driver.request("mcpServer/tool/call", {
                        "threadId": thread, "server": mode, "tool": "change_catalog", "arguments": {},
                    }, 32 + index)
                    assert not changed.get("isError"), changed
                evidence["wire_after_notification"] = audit(34)
                assert all(v["notifications_sent"] > 0 for v in evidence["wire_after_notification"].values()), evidence
                # A server notification has no client-side completion ACK. Give
                # its asynchronous refresh a bounded observation window before
                # spending the one authorized turn, even if it stays stale.
                start = time.monotonic()
                evidence["notification_samples"] = []
                while True:
                    sample = catalogs(1000 + 2 * len(evidence["notification_samples"]))
                    elapsed = time.monotonic() - start
                    evidence["notification_samples"].append({"elapsed_seconds": elapsed, "catalogs": sample})
                    if all("new_tool" in names for names in sample.values()) or elapsed >= 5:
                        break
                    time.sleep(0.1)
                evidence["wire_after_grace"] = audit(38)
            else:
                for service in services.values():
                    service.stop()
                    service.start(2)
                evidence["reload"] = driver.request("config/mcpServer/reload", {}, 5)
            evidence["before_turn"] = catalogs(6)
            if not notification_fixture:
                assert evidence["before_turn"] == evidence["initial"], evidence
            assert not requests, "pre-turn setup must not contact the mocked provider"
            evidence["turn_submissions"] += 1
            driver.request("turn/start", {
                "threadId": thread,
                "input": [{"type": "text", "text": "Run the synthetic catalog check."}],
            }, 8)
            finished = driver.until(lambda event: event.get("method") == "turn/completed")
            evidence["turn_status"] = finished["params"]["turn"]["status"]
            assert evidence["turn_status"] == "completed", finished
            evidence["after_turn"] = catalogs(9)
            if notification_fixture:
                evidence["wire_after_turn"] = audit(36)
            assert len(requests) == 2, "scripted catalog call and final message: two localhost mock responses"
            outputs = [
                block["text"] for row in requests[1]["input"]
                if row.get("type") == "custom_tool_call_output" and row.get("call_id") == "call_catalog"
                for block in row["output"] if block.get("type") == "input_text"
            ]
            records = [json.loads(line.removeprefix("CATALOG_TURN:"))
                       for output in outputs for line in output.splitlines()
                       if line.startswith("CATALOG_TURN:")]
            assert len(records) == 1, outputs
            evidence["inside_turn"] = records[0]
            expected = sorted([*initial_names, "new_tool"])
            assert all(names == expected for names in evidence["after_turn"].values()), evidence
            assert records[0]["catalog"] == sorted(f"mcp__{mode}__{name}" for mode in modes for name in expected), records[0]
            assert set(records[0]["results"]) == set(modes), records[0]
            for result in records[0]["results"].values():
                assert "error" not in result and not result.get("isError"), result
                assert result["content"][0]["text"] == "new catalog", result
        finally:
            try:
                cleanup.close()
            finally:
                evidence["localhost_mock_requests"] = len(requests)
                print("MCP_MOCK_TURN_EVIDENCE " + json.dumps(evidence, sort_keys=True), flush=True)
