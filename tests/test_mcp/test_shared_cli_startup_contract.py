"""Qualified real CLI, four factories, original schemas and shutdown ordering."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
from mcp_types import JSONRPCResponse, jsonrpc_message_adapter

from genesis.db.schema import create_all_tables

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_FIELDS = {"name", "description", "inputSchema", "outputSchema", "annotations", "title", "icons"}


def result(response):
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = next(line[6:] for line in response.text.splitlines()
                    if line.startswith("data: ") and "result" in json.loads(line[6:]))
    else:
        data = response.text
    message = jsonrpc_message_adapter.validate_json(data, by_name=False)
    assert isinstance(message, JSONRPCResponse), message
    return message.result


@pytest.fixture
def notify_receiver(tmp_path, notify_kind):
    # Keep the socket name short even with a descriptive test node directory.
    with tempfile.TemporaryDirectory(prefix="n", dir=tmp_path.parent) as directory:
        address = str(Path(directory) / "n") if notify_kind == "path" else "@genesis-" + uuid.uuid4().hex
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(address if notify_kind == "path" else "\0" + address[1:])
            yield SimpleNamespace(socket=receiver, address=address, directory=Path(directory))


@pytest.fixture
async def shared_install(tmp_path, service):
    home = tmp_path / "home"
    state = home / ".genesis"
    (state / "config").mkdir(parents=True)
    (state / "cc_context_enabled").touch()
    (state / "status.json").write_text(json.dumps({"queue_depths": {"fixture": 7}}))
    secrets = state / "empty.env"
    secrets.write_text("")
    credentials = state / "mcp_credentials"
    credentials.mkdir(mode=0o700)
    (credentials / "forwarder").write_text("f" * 43 + "\n")
    (credentials / "forwarder").chmod(0o600)
    path = tmp_path / "genesis.db"
    async with aiosqlite.connect(path) as db:
        await create_all_tables(db)
        db.row_factory = aiosqlite.Row
        await db.execute("INSERT INTO observations (id, source, type, content, priority, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         ("cli-observation", "recon", "finding", "Fixture finding", "medium", "2026-01-01"))
        await db.execute("INSERT INTO pending_outreach (id, message, category, channel, urgency, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         ("cli-pending", "Fixture message", "fixture", "telegram", "low", "2026-01-01"))
        # Independent direct-SQL oracle, not the service CRUD under test.
        observation = dict(await (await db.execute("SELECT * FROM observations")).fetchone())
        await db.commit()
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    spec = {"unit": f"genesis-mcp-{service}.service", "url": f"http://127.0.0.1:{port}/mcp",
            "db_units": 2, "db_background_units": 1, "db_background_jobs": 4,
            "db_checkout_timeout_s": 1, "tool_concurrency": 2, "max_sessions": 2,
            "session_idle_timeout_s": 30, "max_body_bytes": 65536, "shutdown_grace_s": .25}
    (state / "config/mcp_shared.json").write_text(json.dumps({"servers": {service: spec}}))
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "GENESIS_HOME": str(state),
           "SECRETS_PATH": str(secrets), "GENESIS_DB_PATH": str(path), "GENESIS_REPO_ROOT": str(ROOT),
           "GENESIS_ENABLE_OLLAMA": "0", "GENESIS_MCP_ENV_RECORDER": "0",
           "PYTHONPATH": str(ROOT / "src"), "OTEL_SDK_DISABLED": "true",
           "FASTMCP_CHECK_FOR_UPDATES": "off"}
    if source := os.environ.get("GENESIS_SHARED_CLI_SERVER_SOURCE"):
        env["GENESIS_SHARED_CLI_SERVER_SOURCE"] = source
    events = tmp_path / "events.jsonl"
    return SimpleNamespace(env=env, port=port, events=events, observation=observation)


@pytest.mark.parametrize("service,tool,notify_kind", [
    ("health", "health_status", "path"), ("memory", "observation_query", "path"),
    ("outreach", "outreach_pending", "path"), ("recon", "recon_findings", "path"),
    ("health", "health_status", "abstract"),
], ids=["health-health_status", "memory-observation_query", "outreach-outreach_pending",
        "recon-recon_findings", "health-abstract"])
async def test_real_shared_cli_preserves_catalog_calls_and_resource_shutdown(
    tmp_path, service, tool, notify_kind, shared_install, notify_receiver,
):
    env = dict(shared_install.env, NOTIFY_SOCKET=notify_receiver.address)
    port, events, observation = shared_install.port, shared_install.events, shared_install.observation
    child_script = Path(__file__).with_name("shared_cli_lifecycle_child.py")
    with (tmp_path / "stdout.log").open("w") as out, (tmp_path / "stderr.log").open("w") as err:
        child = subprocess.Popen([sys.executable, str(child_script), service, str(port), str(events)],
                                 cwd="/", env=env, stdout=out, stderr=err)
        try:
            # Existing disposable CLI samples start in ~12s; 60s also covers the
            # observed17.841s cold fixture. No native/model/provider turn runs.
            async with asyncio.timeout(60), httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10,
                headers={"Authorization": "Bearer " + "f" * 43,
                         "X-Genesis-Caller": json.dumps({"dispatched": True, "supervised": False,
                                                        "origin": "external_untrusted"}),
                         "Accept": "application/json, text/event-stream",
                         "MCP-Protocol-Version": "2026-07-28"},
            ) as client:
                while True:
                    assert child.poll() is None, (tmp_path / "stderr.log").read_text()
                    try:
                        status = await client.get("/status")
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(.02)
                assert status.status_code == 200
                assert status.json()["service"] == service and status.json()["enabled"] is True
                meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {}}
                listed = result(await client.post("/mcp", headers={"mcp-method": "tools/list"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}))
                actual = {t["name"]: {k: v for k, v in t.items() if k in CONTRACT_FIELDS} for t in listed["tools"]}
                golden = json.loads((Path(__file__).with_name("fixtures") / "mcp_catalogs_v4.json").read_text())
                # Explicit independent owner split, not the production ancestry
                # classifier: all browser/module tools stay with the local edge.
                expected = {t["name"]: t for t in golden[service] if not (service == "health" and (
                    t["name"].startswith("browser_") or t["name"] in {"module_call", "module_list"}))}
                (tmp_path / "catalog.json").write_text(json.dumps(actual, indent=2))
                if service == "recon":
                    # Independent interpreters can cache list[dict] | dict in
                    # either order. JSON Schema 2020-12 core §10.2 permits either
                    # anyOf order; retain both complete alternatives and every
                    # other byte of the catalog contract.
                    for catalog in (actual, expected):
                        alternatives = catalog["recon_config"]["outputSchema"]["properties"]["result"]["anyOf"]
                        alternatives.sort(key=lambda schema: json.dumps(schema, sort_keys=True))
                assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
                called = result(await client.post("/mcp", headers={"mcp-method": "tools/call", "mcp-name": tool},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": tool, "arguments": {}, "_meta": meta}}))
                assert not called.get("isError"), called
                (tmp_path / "called.json").write_text(json.dumps(called, indent=2))
                payload = called["structuredContent"]
                if service == "health":
                    assert payload["queues"] == {"fixture": 7}, payload
                elif service in {"memory", "recon"}:
                    assert payload == {"result": [observation]}, payload
                else:
                    assert payload == {"items": [{"id": "cli-pending", "category": "fixture",
                        "channel": "telegram", "urgency": "low", "deliver_after": None,
                        "created_at": "2026-01-01", "message_preview": "Fixture message"}],
                        "total": 1, "offset": 0, "limit": 50, "truncated": False}, payload
                assert (await client.get("/status", headers={"Authorization": "Bearer invalid"})).status_code == 401
            assert select.select([notify_receiver.socket], [], [], 0)[0], "No READY after usable listener"
            assert notify_receiver.socket.recv(64) == b"READY=1"
            ready_events = [json.loads(line) for line in events.read_text().splitlines()]
            assert ready_events.index("bound") < ready_events.index("ready-sent"), ready_events
            assert ready_events.count("ready-sent") == 1
            child.send_signal(signal.SIGTERM)
            assert child.wait(timeout=20) == -signal.SIGTERM
            observed = [json.loads(line) for line in events.read_text().splitlines()]
            (tmp_path / "receipt.json").write_text(json.dumps({"service": service, "requests_passed": True,
                "tools": len(actual), "events": observed, "returncode": child.returncode}, indent=2))
            assert "resources-closed" in observed, observed
            assert observed.index("lifespan-stopped") < observed.index("resources-closed")
            assert observed.index("drain-confirmed") < observed.index("resources-closed")
            if service == "memory":
                assert observed.count("reranker-closed") == observed.count("qdrant-closed") == 1
                assert observed.count("cache-closed") == 2
                for event in ("reranker-closed", "cache-closed", "qdrant-closed"):
                    assert observed.index("lifespan-stopped") < observed.index(event) < observed.index("resources-closed")
                    assert observed.index("drain-confirmed") < observed.index(event)
                for index, event in enumerate(observed):
                    if event == "qdrant-finalizer-closed":
                        assert observed.index("resources-closed") < index
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)


@pytest.mark.parametrize("case,service,notify_kind", [
    ("notify-missing", "health", "path"), ("stopping-during-bind", "health", "path"),
    ("bind-error", "health", "path"), ("notify-missing", "memory", "path"),
])
async def test_shared_cli_failed_or_stopping_startup_never_ready(
    tmp_path, service, notify_kind, case, shared_install, notify_receiver,
):
    address = str(notify_receiver.directory / "missing") if case == "notify-missing" else notify_receiver.address
    env = dict(shared_install.env, NOTIFY_SOCKET=address)
    port, events = shared_install.port, shared_install.events
    helper = Path(__file__).with_name("shared_cli_lifecycle_child.py")
    with socket.socket() as occupied, (tmp_path / "stdout.log").open("w") as out, (
        tmp_path / "stderr.log"
    ).open("w") as err:
        if case == "bind-error":
            occupied.bind(("127.0.0.1", port))
            occupied.listen()
        child = subprocess.Popen([sys.executable, str(helper), service, str(port), str(events), case],
                                 cwd="/", env=env, stdout=out, stderr=err)
        try:
            # Same observed startup envelope as the successful CLI cases.
            code = child.wait(timeout=60)
            observed = [json.loads(line) for line in events.read_text().splitlines()]
            (tmp_path / "receipt.json").write_text(json.dumps({"case": case, "events": observed,
                "returncode": code}, indent=2))
            assert code == 0 if case == "stopping-during-bind" else code != 0
            assert ("bound" in observed) == (case != "bind-error"), observed
            if case == "notify-missing":
                assert "FileNotFoundError" in (tmp_path / "stderr.log").read_text()
            assert "ready-sent" not in observed, observed
            assert not select.select([notify_receiver.socket], [], [], 0)[0]
            assert observed.index("lifespan-stopped") < observed.index("resources-closed"), observed
            assert observed.index("drain-confirmed") < observed.index("resources-closed"), observed
            if service == "memory":
                assert observed.count("reranker-closed") == observed.count("qdrant-closed") == 1
                assert observed.count("cache-closed") == 2
                for index, event in enumerate(observed):
                    if event in {"reranker-closed", "cache-closed", "qdrant-closed"}:
                        assert max(observed.index("lifespan-stopped"), observed.index("drain-confirmed")) < index
                        assert index < observed.index("resources-closed")
                    elif event == "qdrant-finalizer-closed":
                        assert observed.index("resources-closed") < index
            if case != "bind-error":
                with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port)):
                    pytest.fail("Startup left its listener open")
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
