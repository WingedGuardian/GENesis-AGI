"""Actual ordinary memory argv and a disposable authenticated shared TCP peer."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import httpx
import pytest

from tests.test_mcp.test_shared_cli_startup_contract import shared_install as shared_install
from tests.test_mcp.test_shared_forwarder import (
    LEGACY,
    MODERN,
    _finish_health_backend,
    _health_frame,
    _health_frontend_environment,
    receive,
    send,
)

ROOT = Path(__file__).resolve().parents[2]


def receipt(env):
    return [json.loads(line) for line in Path(env["LOCAL_MEMORY_TEST_REPORT"]).read_text().splitlines()]


@asynccontextmanager
async def memory_pair(tmp_path, install, *, stage="memory-floor"):
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os,traceback\ntry:\n"
        "    from tests.test_mcp.local_memory_fixture import install\n    install()\n"
        "except BaseException:\n    traceback.print_exc()\n    os._exit(72)\n"
    )
    remote = environment(install, tmp_path / "backend", injection, role="backend", mixed=False)
    remote["LOCAL_MEMORY_TEST_SEED"] = "1"
    local = environment(install, tmp_path / "mixed", injection, role="mixed", mixed=True)
    path = Path(local["GENESIS_HOME"]) / "config/mcp_shared.json"
    config = json.loads(path.read_text())
    config["stage"] = stage
    path.write_text(json.dumps(config))
    owner = child = None
    with (tmp_path / "backend.stderr").open("w") as backend_errors, (tmp_path / "mixed/stderr.log").open("w") as errors:
        try:
            owner = await backend(install, backend_errors, remote)
            child = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "scripts/genesis_mcp_server.py"), "--server", "memory",
                cwd="/", env=local, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=errors,
            )
            yield child, local, remote
            child.stdin.close()
            assert await asyncio.wait_for(child.wait(), 30) == 0
        finally:
            if child is not None and child.returncode is None:
                child.terminate()
                await child.wait()
            await _finish_health_backend(owner, install)


async def invoke(child, request_id, name, arguments, *, era=MODERN):
    await send(child, _health_frame(era, request_id, "tools/call", name=name, arguments=arguments))
    response = await receive(child, timeout=120)
    assert response["id"] == request_id and type(response["id"]) is type(request_id)
    assert "error" not in response and not response["result"].get("isError"), response
    return response["result"].get("structuredContent", response["result"])


@pytest.mark.parametrize("service", ["memory"])
@pytest.mark.parametrize("stage", ["memory-floor", "health-reads"])
@pytest.mark.parametrize("era", [MODERN, LEGACY])
async def test_all_memory_floor_bodies_and_local_write_visibility(tmp_path, service, stage, era, shared_install):
    async with memory_pair(tmp_path, shared_install, stage=stage) as (child, local, remote):
        if era == LEGACY:
            await send(child, _health_frame(era, "init", "initialize", protocolVersion=era,
                                           capabilities={}, clientInfo={"name": "fixture", "version": "1"}))
            initialized = await receive(child, timeout=120)
            assert initialized["id"] == "init" and initialized["result"]["protocolVersion"] == era
            await send(child, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        seeded = next(row for row in receipt(remote) if row["event"] == "seed")
        pending = Path(local["HOME"]) / ".genesis/plan_bookmark_pending.json"
        server_pending = Path(remote["HOME"]) / ".genesis/plan_bookmark_pending.json"
        server_pending.write_text(json.dumps({"title": "Server decoy"}))
        calls = [
            ("observation_query", {"limit": 10}, "cli-observation"),
            ("memory_recall", {"query": "cobalt", "include_graph": False,
                               "expand_query_terms": False, "rerank": False,
                               "corrective": False}, seeded["episodic"]),
            ("memory_expand", {"memory_ids": [seeded["episodic"]]}, seeded["episodic"]),
            ("memory_core_facts", {}, seeded["episodic"]),
            ("memory_stats", {}, '"episodic_count": 1'),
            ("knowledge_recall", {"query": "cobalt", "corrective": False}, seeded["knowledge"]),
            ("knowledge_status", {}, '"total_units": 1'),
            ("procedure_recall", {"task_description": "cobalt"}, seeded["procedure"]),
        ]
        results = {}
        for number, (name, arguments, expected) in enumerate(calls, 3):
            result = await invoke(child, number, name, arguments, era=era)
            results[name] = result
            (tmp_path / "floor-results.json").write_text(json.dumps(results, indent=2))
            assert expected in json.dumps(result), (name, result)
        assert not pending.exists(), "the first eight reads have no marker to consume"

        schemas, cursor = [], None
        for page in range(20):
            params = {} if cursor is None else {"cursor": cursor}
            await send(child, _health_frame(era, "catalog-" + str(page), "tools/list", **params))
            listed = await receive(child)
            assert listed["id"] == "catalog-" + str(page)
            schemas.extend(listed["result"]["tools"])
            cursor = listed["result"].get("nextCursor")
            if cursor is None:
                break
        assert cursor is None and page > 0, "catalog must complete across multiple pages"
        names = [tool["name"] for tool in schemas]
        frozen = json.loads((Path(__file__).with_name("fixtures") / "mcp_catalogs_v4.json").read_text())["memory"]
        assert len(names) == len(set(names)) and set(names) == {tool["name"] for tool in frozen}
        (tmp_path / "catalog.json").write_text(json.dumps(schemas, indent=2))

        # A real local non-floor write and a remote read use the same SQL DB;
        # separate in-memory vector stores are deliberately not a visibility oracle.
        written = await invoke(child, "local-write", "observation_write", {
            "content": "Owned local cobalt write", "source": "fixture", "type": "finding",
        }, era=era)
        result = await invoke(child, "remote-after-write", "observation_query", {"limit": 10}, era=era)
        assert "Owned local cobalt write" in json.dumps(result)
        (tmp_path / "write-result.json").write_text(json.dumps(written, indent=2))

        # Create a marker after the peer was cached. The pending path is sent
        # on every floor call and session resolution must use the caller's tree.
        session = "12345678-caller-owned"
        sessions = pending.parent / "sessions"
        (sessions / session).mkdir(parents=True)
        decoy_sessions = server_pending.parent / "sessions"
        (decoy_sessions / "12345678-server-decoy").mkdir(parents=True)
        pending.write_text(json.dumps({"title": "Caller cobalt plan", "session_id_hint": "12345678"}))
        # Invalid tool arguments and unknown names cannot consume the marker.
        # The shared SDK validates recall; observation_query's existing body
        # rejects a nonpositive limit before its initialization/bookmark guard.
        for name, arguments in [("memory_recall", {}), ("observation_query", {"limit": 0}),
                                ("owned-unregistered-memory-tool", {})]:
            request_id = "invalid-" + name
            await send(child, _health_frame(era, request_id, "tools/call", name=name, arguments=arguments))
            rejected = await receive(child)
            assert rejected["id"] == request_id
            if name == "observation_query":
                assert "limit must be an integer of 1 or more" in json.dumps(rejected)
            else:
                assert "error" in rejected or rejected["result"].get("isError"), rejected
            assert pending.exists()
        await invoke(child, "remote-with-marker", "observation_query", {"limit": 10}, era=era)
        async with asyncio.timeout(120), aiosqlite.connect(shared_install.env["GENESIS_DB_PATH"]) as db:
            while True:
                rows = await (await db.execute("SELECT cc_session_id, topic FROM session_bookmarks WHERE source='plan'")).fetchall()
                if rows:
                    break
                await asyncio.sleep(.02)
            assert rows == [(session, "Caller cobalt plan")]
            assert (await (await db.execute("SELECT COUNT(*) FROM observations WHERE content=?",
                                           ("Owned local cobalt write",))).fetchone())[0] == 1
        assert not pending.exists() and server_pending.exists()

        local_session = "87654321-caller-owned"
        (sessions / local_session).mkdir()
        (decoy_sessions / "87654321-server-decoy").mkdir()
        pending.write_text(json.dumps({"title": "Local cobalt plan", "session_id_hint": "87654321"}))
        await invoke(child, "local-with-marker", "observation_write", {
            "content": "Owned local marker trigger", "source": "fixture", "type": "finding",
        }, era=era)
        async with asyncio.timeout(120), aiosqlite.connect(shared_install.env["GENESIS_DB_PATH"]) as db:
            while True:
                rows = await (await db.execute("SELECT cc_session_id, topic FROM session_bookmarks WHERE source='plan' ORDER BY topic")).fetchall()
                if len(rows) == 2 and any(row["event"] == "synthetic-extraction" for row in receipt(local)):
                    break
                await asyncio.sleep(.02)
            assert rows == [(session, "Caller cobalt plan"), (local_session, "Local cobalt plan")]
        assert not pending.exists() and server_pending.exists()
        await invoke(child, "remote-after-local-marker", "observation_query", {"limit": 10}, era=era)

        local_guards = [row["request_id"] for row in receipt(local) if row["event"] == "body-guard"]
        assert local_guards == ["local-write", "local-with-marker"]
        remote_guards = [row["request_id"] for row in receipt(remote) if row["event"] == "body-guard"]
        assert remote_guards == [*range(3, 11), "remote-after-write", "remote-with-marker", "remote-after-local-marker"]
        wire = [row for row in receipt(local) if row["event"] == "http-request" and (row["body"] or {}).get("method") == "tools/call"]
        assert [row["body"]["params"]["name"] for row in wire] == [name for name, _, _ in calls] + [
            "observation_query", "memory_recall", "observation_query", "observation_query", "observation_query"]
        assert all(row["path"] == "/mcp/v2" for row in wire)
        assert all(row["body"]["params"]["_meta"]["genesis/planBookmarkPending"]["path"] == str(pending) for row in wire)
        assert [row["event"] for row in receipt(remote)].count("synthetic-extraction") == 1
        assert [row["event"] for row in receipt(local)].count("synthetic-extraction") == 1
    events = [row["event"] for row in receipt(local)]
    assert events.count("lifespan-enter") == events.count("lifespan-returned") == 1
    assert [row["episodic"] for row in receipt(local) if row["event"] == "vectors-before-close"] == [1]
    assert [row["episodic"] for row in receipt(remote) if row["event"] == "vectors-before-close"] == [2]


def environment(install, directory, injection, *, role, mixed):
    # Reuse the existing service-neutral private-home/credential setup. Its
    # legacy health observer variables are not read by the memory observer.
    env = _health_frontend_environment(install, directory, injection, role=role, mixed=mixed)
    config_path = Path(env["GENESIS_HOME"]) / "config/mcp_shared.json"
    config = json.loads(config_path.read_text())
    config["stage"] = "memory-floor"
    config_path.write_text(json.dumps(config))
    return dict(env, LOCAL_MEMORY_TEST_PORT=str(install.port),
                LOCAL_MEMORY_TEST_REPORT=str(directory / "receipt.jsonl"),
                LOCAL_MEMORY_TEST_ROLE=role, LOCAL_MEMORY_TEST_MIXED="1" if mixed else "0")


async def backend(install, errors, env):
    child = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name("shared_cli_lifecycle_child.py")),
        "memory", str(install.port), str(install.events), cwd="/", env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=errors,
    )
    headers = {"Authorization": "Bearer " + "f" * 43,
               "X-Genesis-Caller": json.dumps({"dispatched": True, "supervised": False,
                                              "origin": "external_untrusted"})}
    try:
        # Existing ordinary frontend fixture deadlines; no production timeout.
        async with asyncio.timeout(180), httpx.AsyncClient(headers=headers, trust_env=False) as client:
            while True:
                assert child.returncode is None
                try:
                    if (await client.get(f"http://127.0.0.1:{install.port}/status")).status_code == 200:
                        return child
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(.02)
    except BaseException:
        if child.returncode is None:
            child.terminate()
            await child.wait()
        raise


@pytest.mark.parametrize("service", ["memory"])
async def test_ordinary_memory_read_uses_same_backend_v2_with_absent_marker(tmp_path, service, shared_install):
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os,traceback\ntry:\n"
        "    from tests.test_mcp.local_memory_fixture import install\n    install()\n"
        "except BaseException:\n    traceback.print_exc()\n    os._exit(72)\n"
    )
    backend_env = environment(shared_install, tmp_path / "backend", injection, role="backend", mixed=False)
    env = environment(shared_install, tmp_path / "mixed", injection, role="mixed", mixed=True)
    server_pending = Path(backend_env["HOME"]) / ".genesis/plan_bookmark_pending.json"
    server_pending.write_text(json.dumps({"title": "Server decoy"}))
    pending = Path(env["HOME"]) / ".genesis/plan_bookmark_pending.json"
    child = owner = None
    with (tmp_path / "backend.stderr").open("w") as backend_errors, (tmp_path / "mixed/stderr.log").open("w") as errors:
        try:
            owner = await backend(shared_install, backend_errors, backend_env)
            child = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "scripts/genesis_mcp_server.py"), "--server", "memory",
                cwd="/", env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errors,
            )
            await send(child, _health_frame(MODERN, 3, "tools/call", name="observation_query", arguments={"limit": 10}))
            called = await receive(child, timeout=120)
            assert type(called["id"]) is int and called["id"] == 3
            assert [row["id"] for row in called["result"]["structuredContent"]["result"]] == ["cli-observation"]
            child.stdin.close()
            assert await asyncio.wait_for(child.wait(), 30) == 0
            receipts = [json.loads(line) for line in Path(env["LOCAL_MEMORY_TEST_REPORT"]).read_text().splitlines()]
            wire = [row for row in receipts if row["event"] == "http-request" and (row["body"] or {}).get("method") == "tools/call"]
            assert len(wire) == 1 and wire[0]["path"] == "/mcp/v2", "ordinary memory floor did not use the shared v2 backend"
            assert wire[0]["body"]["id"] == 3
            metadata = wire[0]["body"]["params"]["_meta"]
            assert metadata["fixture.local"] == {"kept": True}
            assert metadata["genesis/planBookmarkPending"] == {"version": 1, "path": str(pending)}
            assert [row["pending_enabled"] for row in receipts if row["event"] == "memory-init"] == [True]
            assert not [row for row in receipts if row["event"] == "body-guard"]
            remote = [json.loads(line) for line in Path(backend_env["LOCAL_MEMORY_TEST_REPORT"]).read_text().splitlines()]
            assert [row["request_id"] for row in remote if row["event"] == "body-guard"] == [3]
            assert [row["pending_enabled"] for row in remote if row["event"] == "memory-init"] == [False]
            assert not pending.exists() and server_pending.exists()
        finally:
            if child is not None and child.returncode is None:
                child.terminate()
                await child.wait()
            await _finish_health_backend(owner, shared_install)
