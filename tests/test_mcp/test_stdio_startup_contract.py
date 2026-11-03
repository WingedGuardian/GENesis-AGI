"""Run real standalone launchers with fully disposable runtime state."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import aiosqlite
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from genesis.db.schema import create_all_tables

_ROOT = Path(__file__).resolve().parents[2]
_CASES = ["health", "memory", "outreach", "recon", "discord-bot", "peer",
          "external-health", "external-memory"]


@pytest.fixture
async def stdio_install(tmp_path):
    # The capsule carries byte-identical launcher scripts and a symlink to the
    # source under test. Its .venv selects THIS test's isolated interpreter,
    # so the shell/external launcher cannot fall back to the live venv.
    source = Path(os.environ.get("GENESIS_MCP_TEST_SOURCE", _ROOT)).resolve()
    capsule = tmp_path / "install"
    (capsule / "scripts").mkdir(parents=True)
    (capsule / ".claude" / "mcp").mkdir(parents=True)
    (capsule / "src").symlink_to(source / "src", target_is_directory=True)
    (capsule / ".venv").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    for script in ("genesis_mcp_server.py", "genesis_peer_mcp.py", "codex_external_mcp.py"):
        shutil.copy2(source / "scripts" / script, capsule / "scripts" / script)
    shutil.copy2(source / ".claude" / "mcp" / "run-mcp-server", capsule / ".claude" / "mcp")
    shutil.copytree(source / "config", capsule / "config")
    home = tmp_path / "home"
    state = home / ".genesis"
    state.mkdir(parents=True)
    (home / "tmp").mkdir()
    (state / "secrets.env").write_text("")
    (state / "cc_context_enabled").touch()
    (state / "status.json").write_text(json.dumps({"queue_depths": {"fixture": 1}}))
    db_path = capsule / "data" / "genesis.db"
    db_path.parent.mkdir()
    async with aiosqlite.connect(db_path) as db:
        await create_all_tables(db)
        await db.commit()
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os, traceback\ntry:\n"
        "    from tests.mcp_stdio_fixture import install\n    install()\n"
        "except BaseException:\n    traceback.print_exc()\n    os._exit(72)\n"
    )
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "GENESIS_HOME": str(state),
        "GENESIS_DB_PATH": str(db_path), "GENESIS_REPO_ROOT": str(capsule),
        "PYTHONPATH": os.pathsep.join([str(injection), str(source / "src"), str(_ROOT)]),
        "FASTMCP_CHECK_FOR_UPDATES": "off", "FASTMCP_SHOW_SERVER_BANNER": "false",
        "GENESIS_ENABLE_OLLAMA": "0", "GENESIS_RECALL_READ_POOL_OFF": "1",
        "GENESIS_MCP_ENV_RECORDER": "0", "DISCORD_BOT_TOKEN": "synthetic-test-token",
        "GENESIS_PEER_URL": "https://peer.example.invalid", "GENESIS_PEER_TOKEN": "fixture",
        "GENESIS_MCP_LOG_DIR": str(home / "tmp"),
        "GENESIS_STDIO_TEST_REPORT": str(tmp_path / "child-report.json"),
    }
    return capsule, env


@pytest.mark.parametrize("server", _CASES)
async def test_real_launcher_starts_lists_and_calls(server, stdio_install, tmp_path):
    capsule, env = stdio_install
    external = server.startswith("external-")
    name = server.removeprefix("external-")
    script = "codex_external_mcp.py" if external else "genesis_mcp_server.py"
    args = [str(capsule / "scripts" / script), "--server", name]
    if name == "peer":
        args = [str(capsule / "scripts" / "genesis_peer_mcp.py")]
    params = StdioServerParameters(command=sys.executable, args=args, env=env)
    started = time.perf_counter()
    with (tmp_path / "server.stderr").open("w") as errors:
        async with (
            stdio_client(params, errlog=errors) as (read, write),
            ClientSession(read, write) as client,
        ):
            initialized = await client.initialize()
            tools = (await client.list_tools()).tools
            startup = time.perf_counter() - started
            names = {tool.name for tool in tools}
            assert names
            if external:
                from genesis.mcp.external_profiles import profile_tools

                assert names == profile_tools(name, "external")
                forbidden = "settings_list" if name == "health" else "reference_export"
                denied = (await client.call_tool(forbidden, {})).model_dump(by_alias=True)
                assert denied["isError"] is True
                assert "unavailable to this external client profile" in denied["content"][0]["text"]
            else:
                frozen = json.loads((Path(__file__).with_name("fixtures") / "mcp_catalogs.json").read_text())
                assert names == {tool["name"] for tool in frozen[name]}
            calls = {
                "health": ("health_status", {}),
                "memory": ("observation_query", {}),
                "outreach": ("outreach_pending", {}),
                "recon": ("recon_findings", {}),
                "discord-bot": ("fetch_messages", {"channel_id": "123"}),
                "peer": ("genesis_health", {}),
            }
            tool, arguments = calls[name]
            result = await client.call_tool(tool, arguments)
            payload = result.model_dump(by_alias=True, exclude_none=True)
            assert not payload.get("isError", False), payload
            if name in {"memory", "recon"}:
                assert payload["structuredContent"] == {"result": []}
            else:
                content = json.loads(payload["content"][0]["text"])
                if name == "health":
                    assert content["queues"]["fixture"] == 1
                elif name == "outreach":
                    assert content == {"items": [], "total": 0, "offset": 0,
                                       "limit": 50, "truncated": False}
                elif name == "discord-bot":
                    assert content == {"messages": [], "count": 0}
                else:
                    assert content == {"status": "fixture"}
    child = json.loads((tmp_path / "child-report.json").read_text())
    assert child["entries"] == child["exits"] == 1
    assert child["listed_without_worker"] is True
    assert child["source"] == str((capsule / "src" / "genesis" / "__init__.py").resolve())
    # Receipts preserve measurements without putting install-specific absolute
    # paths or machine measurements in the committed fixture files.
    receipt = {
        "server": server, "startup_seconds": startup, "tool_names": sorted(names),
        "protocol": initialized.model_dump(by_alias=True)["protocolVersion"],
        "executable": sys.executable,
        "child": child,
    }
    (tmp_path / "stdio-receipt.json").write_text(json.dumps(receipt, indent=2))
    evidence = os.environ.get("GENESIS_STDIO_EVIDENCE_DIR")
    if evidence:
        destination = Path(evidence)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f"stdio-{os.getpid()}-{server}.json").write_text(json.dumps(receipt, indent=2))
