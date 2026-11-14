"""Real ordinary stdio/TCP cancellation and refusal without local replay."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import sys
from contextlib import asynccontextmanager

import aiosqlite
import pytest

from tests.test_mcp.test_ordinary_memory_frontend import ROOT, backend, environment, invoke, receipt
from tests.test_mcp.test_shared_cli_startup_contract import shared_install as shared_install
from tests.test_mcp.test_shared_forwarder import (
    LEGACY,
    MODERN,
    _finish_health_backend,
    _health_frame,
    receive,
    send,
)


@asynccontextmanager
async def pair(tmp_path, install, fault):
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os,traceback\ntry:\n"
        "    from tests.test_mcp.local_memory_fixture import install\n    install()\n"
        "    from tests.test_mcp.local_memory_faults import install\n    install()\n"
        "except BaseException:\n    traceback.print_exc()\n    os._exit(72)\n"
    )
    remote = environment(install, tmp_path / "backend", injection, role="backend", mixed=False)
    local = environment(install, tmp_path / "mixed", injection, role="mixed", mixed=True)
    for env in [remote, local]:
        env["LOCAL_MEMORY_TEST_FAULT"] = fault
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


async def observed(env, event, route, count):
    async with asyncio.timeout(120):
        while True:
            if sum(row["event"] == event and row.get("route") == route for row in receipt(env)) == count:
                return
            await asyncio.sleep(.02)


async def initialize(child, era):
    if era == LEGACY:
        await send(child, _health_frame(era, "init", "initialize", protocolVersion=era,
                                       capabilities={}, clientInfo={"name": "fixture", "version": "1"}))
        assert (await receive(child, timeout=120))["result"]["protocolVersion"] == era
        await send(child, {"jsonrpc": "2.0", "method": "notifications/initialized"})


@pytest.mark.parametrize("service", ["memory"])
@pytest.mark.parametrize("era", [MODERN, LEGACY])
async def test_memory_cancel_then_eof_settles_both_routes_without_replay(tmp_path, service, era, shared_install):
    async with pair(tmp_path, shared_install, "cancel") as (child, local, remote):
        await initialize(child, era)
        for name, arguments, route, env in [
            ("observation_query", {"source": "owned-memory-hold"}, "remote", remote),
            ("observation_write", {"content": "owned-memory-hold", "source": "fixture", "type": "finding"}, "local", local),
        ]:
            # Reuse integer zero after cancellation. Concurrent IDs below use
            # a nonnumeric string: the qualified SDK correlates "0" and 0 alike.
            await send(child, _health_frame(era, 0, "tools/call", name=name, arguments=arguments))
            await observed(env, "held", route, 1)
            await send(child, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 0}})
            await invoke(child, "barrier-" + route, "observation_query", {"limit": 10}, era=era)
            await observed(env, "hold-ended", route, 1)
        for request_id, name, arguments, env, route in [
            (0, "observation_query", {"source": "owned-memory-hold"}, remote, "remote"),
            ("local-held", "observation_write", {"content": "owned-memory-hold", "source": "fixture", "type": "finding"}, local, "local"),
        ]:
            await send(child, _health_frame(era, request_id, "tools/call", name=name, arguments=arguments))
            await observed(env, "held", route, 2)
        child.stdin.close()
        assert await asyncio.wait_for(child.wait(), 30) == 0
        await observed(remote, "hold-ended", "remote", 2)
        await observed(local, "hold-ended", "local", 2)
        async with aiosqlite.connect(shared_install.env["GENESIS_DB_PATH"]) as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM observations WHERE content='owned-memory-hold'")).fetchone())[0] == 0
        events = [row["event"] for row in receipt(local)]
        assert max(i for i, event in enumerate(events) if event == "hold-ended") < events.index("lifespan-returned")


@pytest.mark.parametrize("service", ["memory"])
@pytest.mark.parametrize("fault", ["feature", "catalog"])
async def test_memory_preflight_refuses_without_falling_back(tmp_path, service, fault, shared_install):
    async with pair(tmp_path, shared_install, fault) as (child, local, remote):
        await send(child, _health_frame(MODERN, 3, "tools/call", name="observation_query", arguments={"limit": 10}))
        refused = await receive(child, timeout=120)
        assert refused["id"] == 3 and "error" in refused
        assert not [row for row in receipt(local) + receipt(remote) if row["event"] == "body-guard"]
        assert len([row for row in receipt(local) if row["event"] == "status-fault"]) == 1
        calls = [row for row in receipt(local) if row["event"] == "http-request" and (row["body"] or {}).get("method") == "tools/call"]
        assert not calls
