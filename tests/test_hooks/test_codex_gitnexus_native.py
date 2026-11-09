"""Opt-in project GitNexus qualification: real server, private registry, fake model."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

from tests.test_hooks.native_codex import AppServer, configure, fixture_provider, run_native

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
    reason="native Codex and pinned GitNexus are separate opt-in requirements",
)
REPO = Path(__file__).resolve().parents[2]
DEFERRED_MUTATORS = ("rename", "group_sync")
SOURCES = (".codex/config.toml", ".claude/mcp/run-gitnexus", "scripts/lib/gitnexus_version.sh")


def _configure(root, url, allowed, *, required=True):
    env = configure(root, url, "allow")
    home_config = root / "codex/config.toml"
    text = home_config.read_text().split("[[hooks.PreToolUse]]")[0]
    binary = os.environ.get("GENESIS_GITNEXUS_TEST_BIN") or shutil.which("gitnexus")
    node = shutil.which("node")
    assert binary and Path(binary).is_absolute() and Path(binary).is_file(), "GitNexus required"
    assert node and Path(node).is_absolute(), "Node required"
    # Scripted first-turn actions need a startup barrier, just like a CLI job
    # explicitly requiring GitNexus. This changes only the disposable profile.
    text += (
        f"\n[mcp_servers.gitnexus]\nrequired={str(required).lower()}\n"
        'command="missing-global-fixture-command"\nargs=["global-only"]\n'
    )
    text += "[mcp_servers.gitnexus.env]\n"
    for name, value in {
        "GITNEXUS_HOME": str(root / "private-registry"),
        "GITNEXUS_BIN": binary,
        "GITNEXUS_NODE_BIN": node,
        "GENESIS_GITNEXUS_FIXTURE_SENTINEL": "retained",
    }.items():
        text += f"{name}={json.dumps(value)}\n"
    text += "\n[mcp_servers.genesis-health]\nenabled=false\n[mcp_servers.genesis-memory]\nenabled=false\n"
    text += f'\n[projects.{json.dumps(str(root / "workspace"))}]\ntrust_level="trusted"\n'
    home_config.write_text(text)
    hashes = {}
    for name in SOURCES:
        target = root / "workspace" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / name, target)
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    if not allowed:
        config = root / "workspace/.codex/config.toml"
        config.write_text(config.read_text().replace('  "list_repos",\n', ""))
    fixture_hashes = {
        name: hashlib.sha256((root / "workspace" / name).read_bytes()).hexdigest()
        for name in SOURCES
    }
    return env, {"source": hashes, "fixture": fixture_hashes}


def _assert_empty_registry(result):
    assert not result.get("isError") and "error" not in result, result
    text = result["content"][0]["text"]
    data, end = json.JSONDecoder().raw_decode(text)
    assert text[end:].startswith("\n\n---\n**Next:**"), text[end:]
    assert data["repositories"] == [] and data["pagination"]["total"] == 0, data
    assert data["pagination"]["hasMore"] is False


def _expect_disabled_tool(driver, thread_id, tool, request_id):
    """Capture only this expected refusal without weakening success-path RPCs."""
    driver.send("mcpServer/tool/call", {
        "threadId": thread_id, "server": "gitnexus", "tool": tool, "arguments": {},
    }, request_id)
    deadline = time.monotonic() + 30
    while True:
        event = driver.pending.get(timeout=max(0, deadline - time.monotonic()))
        driver.events.append(event)
        assert "closed" not in event and "invalid_json" not in event, event
        assert not ("method" in event and "id" in event), event
        if event.get("id") == request_id and "method" not in event:
            assert "result" not in event and "error" in event, event
            assert event["error"]["message"] == (
                f"tool '{tool}' is disabled for MCP server 'gitnexus'"
            ), event
            return event
        assert "error" not in event and "id" not in event, event


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("allowed", [True, False])
def test_project_gitnexus_native_call_and_allowlist(tmp_path, client, allowed):
    binary = os.environ.get("GENESIS_CODEX_CLI_BIN") or shutil.which("codex")
    app_binary = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN") or binary
    assert binary and app_binary, "native binaries required"
    root = tmp_path / "portable space ü"
    root.mkdir()
    item = {
        "type": "custom_tool_call",
        "id": "ct_fixture",
        "call_id": "call_fixture",
        "namespace": "functions",
        "name": "exec",
        "input": (
            'const catalog = ALL_TOOLS.filter(t => t.name.startsWith("mcp__gitnexus__"))'
            ".map(t => t.name).sort(); let result; "
            "try { result = await tools.mcp__gitnexus__list_repos({}); } "
            "catch (error) { result = {error: String(error)}; } "
            "const deferred = {}; "
            'for (const name of ["rename", "group_sync"]) { '
            'const key = "mcp__gitnexus__" + name; const type = typeof tools[key]; '
            'try { deferred[name] = {type, result: await tools[key]({})}; } '
            'catch (error) { deferred[name] = {type, error: String(error)}; } } '
            'text("GITNEXUS_FIXTURE:" + JSON.stringify({catalog, result, deferred}));'
        ),
    }
    evidence = {"client": client, "allowed": allowed}
    try:
        with fixture_provider(item) as (url, requests):
            env, hashes = _configure(root, url, allowed, required=client == "app")
            evidence["source_hashes"] = hashes
            evidence["requests"] = requests
            evidence["version"] = subprocess.check_output([binary, "--version"], text=True).strip()
            driver = AppServer(app_binary, root, env)
            try:
                driver.initialize()
                effective = driver.request(
                    "config/read",
                    {"includeLayers": False, "cwd": str(root / "workspace")},
                    2,
                )["config"]
                evidence["effective_config"] = effective
                servers = effective["mcp_servers"]
                gitnexus = servers["gitnexus"]
                assert gitnexus["command"] == "bash"
                assert gitnexus["args"] == [".claude/mcp/run-gitnexus", "mcp"]
                assert gitnexus["cwd"] == "."
                assert gitnexus["env"]["GENESIS_GITNEXUS_FIXTURE_SENTINEL"] == "retained"
                assert not servers["genesis-health"]["enabled"]
                assert not servers["genesis-memory"]["enabled"]
                expected = tomllib.loads((root / "workspace/.codex/config.toml").read_text())
                assert (
                    gitnexus["enabled_tools"]
                    == expected["mcp_servers"]["gitnexus"]["enabled_tools"]
                )
                assert gitnexus.get("required", False) is (client == "app")
                thread = driver.request(
                    "thread/start",
                    {"cwd": str(root / "workspace"), "ephemeral": True},
                    3,
                )["thread"]
                catalog = driver.request(
                    "mcpServerStatus/list",
                    {"threadId": thread["id"], "serverName": "gitnexus"},
                    4,
                )
                evidence["server_catalog"] = catalog
                assert catalog["nextCursor"] is None and len(catalog["data"]) == 1
                server = catalog["data"][0]
                assert server["runtimeStatus"] == "connected" and server["toolsError"] is None
                assert server["serverInfo"]["version"] == "1.6.12"
                assert set(gitnexus["enabled_tools"]) == set(server["tools"])
            finally:
                driver.close()
            cli_binary = binary
            if client == "cli":
                wrapper = root / "required-codex"
                wrapper.write_text(
                    f"#!/bin/sh\nexec {shlex.quote(binary)} "
                    '-c mcp_servers.gitnexus.required=true "$@"\n'
                )
                wrapper.chmod(0o700)
                cli_binary = str(wrapper)
                evidence["cli_override_wrapper_sha256"] = hashlib.sha256(
                    wrapper.read_bytes()
                ).hexdigest()
            events, stderr = run_native(
                cli_binary if client == "cli" else app_binary, client, root, env, evidence
            )
            evidence.update(events=events, stderr=stderr)
        assert len(requests) == 2, evidence
        outputs = [
            block["text"]
            for row in requests[1]["input"]
            if row.get("type") == "custom_tool_call_output" and row.get("call_id") == "call_fixture"
            for block in row["output"]
            if block.get("type") == "input_text"
        ]
        records = [
            json.loads(line.removeprefix("GITNEXUS_FIXTURE:"))
            for output in outputs
            for line in output.splitlines()
            if line.startswith("GITNEXUS_FIXTURE:")
        ]
        assert len(records) == 1, outputs
        returned = records[0]
        expected_names = expected["mcp_servers"]["gitnexus"]["enabled_tools"]
        assert returned["catalog"] == sorted("mcp__gitnexus__" + name for name in expected_names)
        for name in DEFERRED_MUTATORS:
            refusal = returned["deferred"][name]
            assert refusal["type"] == "undefined", refusal
            assert "not a function" in refusal.get("error", ""), refusal
        if allowed:
            _assert_empty_registry(returned["result"])
        else:
            assert "not a function" in returned["result"]["error"], returned
        assert not list((root / "codex/.tmp").glob("plugins-clone-*"))
    finally:
        (root / "native-evidence.json").write_text(json.dumps(evidence, indent=2))


def test_optional_project_connects_and_calls_without_model(tmp_path):
    binary = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN") or shutil.which("codex")
    assert binary, "native app-server required"
    root = tmp_path / "optional space ü"
    root.mkdir()
    evidence = {}
    with fixture_provider({}) as (url, requests):
        env, hashes = _configure(root, url, True, required=False)
        evidence["source_hashes"] = hashes
        driver = AppServer(binary, root, env)
        try:
            driver.initialize()
            thread = driver.request(
                "thread/start",
                {"cwd": str(root / "workspace"), "ephemeral": True},
                2,
            )["thread"]
            catalog = driver.request(
                "mcpServerStatus/list",
                {"threadId": thread["id"], "serverName": "gitnexus"},
                3,
            )
            evidence["catalog"] = catalog
            assert catalog["nextCursor"] is None and len(catalog["data"]) == 1
            server = catalog["data"][0]
            assert server["runtimeStatus"] == "connected" and server["toolsError"] is None
            expected = tomllib.loads((root / "workspace/.codex/config.toml").read_text())
            selected = expected["mcp_servers"]["gitnexus"]["enabled_tools"]
            assert len(selected) == 15 and set(server["tools"]) == set(selected)
            result = driver.request(
                "mcpServer/tool/call",
                {
                    "threadId": thread["id"],
                    "server": "gitnexus",
                    "tool": "list_repos",
                    "arguments": {},
                },
                4,
            )
            evidence["result"] = result
            _assert_empty_registry(result)
            registry = root / "private-registry"
            before = {
                str(path.relative_to(registry)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in registry.rglob("*") if path.is_file()
            }
            evidence["refusals"] = {}
            for request_id, tool in enumerate(DEFERRED_MUTATORS, 5):
                evidence["refusals"][tool] = _expect_disabled_tool(
                    driver, thread["id"], tool, request_id
                )
            after = {
                str(path.relative_to(registry)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in registry.rglob("*") if path.is_file()
            }
            assert after == before, "disabled calls changed the private registry"
            result_after = driver.request("mcpServer/tool/call", {
                "threadId": thread["id"], "server": "gitnexus",
                "tool": "list_repos", "arguments": {},
            }, 7)
            evidence["result_after"] = result_after
            _assert_empty_registry(result_after)
            assert not requests, "catalog/call probe must not invoke a model"
        finally:
            driver.close()
            (root / "native-evidence.json").write_text(json.dumps(evidence, indent=2))
