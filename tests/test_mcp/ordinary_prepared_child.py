"""Real ordinary preparation/lifespan with the disposable stdio dependency fence."""

import hashlib
import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import mcp_types as types
from fastmcp.client.transports.base import TransportOptions
from fastmcp.client.transports.memory import FastMCPTransport
from fastmcp.server.middleware import Middleware
from mcp import ClientSession
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

root = Path(sys.argv[1])
case = json.loads((root / "case.json").read_text())
capsule = Path(case["capsule"])
service, profile, era = case["service"], case["profile"], case["era"]
assert service in {"health", "memory"} and profile in {None, "external", "validator"}
spec = importlib.util.spec_from_file_location("prepared_ordinary_fixture", capsule / "scripts/genesis_mcp_server.py")
wrapper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wrapper
spec.loader.exec_module(wrapper)
if case["missing"]:
    wrapper._DEFAULT_DB = root / "absent.sqlite"

runner_calls = []
wrapper._run_mcp = lambda server, options: runner_calls.append((server, options))
options = {"external_profile": profile}
if case["mode"] == "oracle":
    getattr(wrapper, "_bootstrap_" + service)({"transport": "stdio"}, **options)
    server = runner_calls[0][0] if runner_calls else None
else:
    server = getattr(wrapper, "_prepare_" + service)(**options)
    assert not runner_calls, "preparation ran the transport"

report = {"missing": server is None, "events": [], "handler_ids": [],
          "runner_calls": len(runner_calls), "source": str(capsule / "scripts/genesis_mcp_server.py"),
          "script_sha256": hashlib.sha256((capsule / "scripts/genesis_mcp_server.py").read_bytes()).hexdigest()}


class RetainedSession(ClientSession):
    def __init__(self, read_stream, write_stream, **kwargs):
        self.relay = JSONRPCDispatcher(read_stream, write_stream)
        super().__init__(dispatcher=self.relay, **kwargs)


async def main():
    if server is None:
        (root / "result.json").write_text(json.dumps(report))
        return
    from genesis.mcp import memory

    if case["pool"] == "failed":
        from genesis.db.connection import ReadConnectionPool

        async def fail_open(self):
            raise RuntimeError("synthetic read-pool opening failure")

        ReadConnectionPool.open = fail_open
    original_lifespan = server._lifespan

    @asynccontextmanager
    async def lifespan(instance):
        report["events"].append("enter")
        try:
            async with original_lifespan(instance) as state:
                yield state
        finally:
            report["events"].append("exit")

    class Observe(Middleware):
        async def on_call_tool(self, context, call_next):
            # Qualified SDK observer only; FastMCP's wrapper stringifies IDs.
            report["handler_ids"].append(context.fastmcp_context.request_context._srctx.request_id)
            return await call_next(context)

    server._lifespan = lifespan
    server.add_middleware(Observe())
    pool = None
    async with FastMCPTransport(server).connect_session(
        transport_options=TransportOptions(session_class=RetainedSession),
    ) as session:
        meta = {"io.modelcontextprotocol/protocolVersion": era,
                "io.modelcontextprotocol/clientCapabilities": {}} if era == "2026-07-28" else {}
        if era == "2026-07-28":
            raw = await session.relay.send_raw_request("server/discover", {"_meta": meta})
            session.adopt(types.DiscoverResult.model_validate(raw))
        else:
            raw = await session.relay.send_raw_request("initialize", {
                "protocolVersion": era, "capabilities": {},
                "clientInfo": {"name": "ordinary-fixture", "version": "1"},
            }, {"cancel_on_abandon": False})
            session.adopt(types.InitializeResult.model_validate(raw))
            await session.relay.notify("notifications/initialized", None)
        report["protocol"] = session.protocol_version
        report["catalog"] = await session.relay.send_raw_request("tools/list", {"_meta": meta})
        tool = "health_status" if service == "health" else "observation_query"
        report["calls"] = []
        for request_id in [3, "ordinary-original"]:
            report["calls"].append(await session.relay.send_raw_request("tools/call", {
                "name": tool, "arguments": {}, "_meta": meta,
            }, {"request_id": request_id}))
        if profile is not None:
            forbidden = "settings_list" if service == "health" else "reference_export"
            report["denied"] = await session.relay.send_raw_request("tools/call", {
                "name": forbidden, "arguments": {}, "_meta": meta,
            }, {"request_id": "forbidden"})
        if service == "memory":
            report["pending_enabled"] = memory._process_pending_bookmarks
            pool = memory._retriever._read_pool
            report["read_pool"] = pool is not None
            if pool is not None:
                async with pool.acquire() as connection:
                    cursor = await connection.execute("SELECT id FROM observations")
                    report["pool_rows"] = [row[0] for row in await cursor.fetchall()]
                    await cursor.close()
    if pool is not None:
        from genesis.db.connection import ReadPoolClosed

        try:
            async with pool.acquire():
                raise AssertionError("closed pool admitted a reader")
        except ReadPoolClosed:
            report["pool_closed"] = True
    (root / "result.json").write_text(json.dumps(report))


anyio.run(main)
