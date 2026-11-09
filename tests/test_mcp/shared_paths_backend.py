"""Actual ingestion wrappers, owned files and explicit provider boundaries."""

import asyncio
import importlib
import json
import socket
import subprocess
import sys
from pathlib import Path

root, port = Path(sys.argv[1]), int(sys.argv[2])
service = sys.argv[3] if len(sys.argv) > 3 else "memory"


def refuse(*args, **kwargs):
    raise PermissionError("path fixture refuses external effects")


socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = refuse


class NoProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        refuse()


subprocess.Popen = NoProcess

import uvicorn  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

from genesis.caller_credentials import CallerResolver  # noqa: E402
from genesis.db.request_database import RequestDatabase  # noqa: E402
from genesis.knowledge.manifest import ManifestManager  # noqa: E402
from genesis.knowledge.orchestrator import KnowledgeOrchestrator  # noqa: E402
from genesis.knowledge.processors.registry import ContentProcessorRegistry  # noqa: E402
from genesis.knowledge.processors.text import TextProcessor  # noqa: E402
from genesis.mcp.shared_http import SharedHTTPConfig, build_http_app  # noqa: E402
from genesis.mcp.shared_server import SharedServer  # noqa: E402
from genesis.observability.mcp_middleware import install_instrumentation  # noqa: E402
from genesis.observability.provider_activity import ProviderActivityTracker  # noqa: E402

knowledge = importlib.import_module("genesis.mcp.memory.knowledge")


def record(event, **values):
    with (root / "path-events.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": event, **values}) + "\n")
        stream.flush()


class Distill:
    last_extraction_ratio = 1

    async def distill(self, content, **kwargs):
        record("distill", text=content.text, source=content.source_path)
        return []  # Explicit model/storage boundary; no provider or vector write.


async def main():
    registry = ContentProcessorRegistry()
    registry.register_extensions(TextProcessor(), [".txt"])
    manifest = ManifestManager(root=root / "manifest")
    orchestrator = KnowledgeOrchestrator(
        registry=registry, manifest=manifest, distillation=Distill(),
    )

    def configured_orchestrator():
        record("orchestrator")
        return orchestrator

    knowledge._get_orchestrator = configured_orchestrator
    database = RequestDatabase(root / "genesis.db", max_units=2,
                               max_background_units=1, max_background_jobs=4)
    server = FastMCP("caller-path-fixture")
    if (root / "tool-cases.json").exists():
        from shared_paths_tools import configure_tools

        tools = await configure_tools(root, service, record)
        cases = json.loads((root / "tool-cases.json").read_text())
        # Ordinary wrappers run before build_http_app enters shared-service mode.
        import os

        os.chdir(root / "caller")
        oracle = []
        for case in cases:
            ordinary_arguments = dict(case["arguments"])
            if case["label"] == "plan":
                task = importlib.import_module("genesis.mcp.health.task_tools")
                ordinary_arguments["intake_token"] = (await task._impl_intake_complete())["token"]
                case["arguments"]["intake_token"] = (await task._impl_intake_complete())["token"]
            record("phase", label=case["label"], mode="ordinary")
            oracle.append(await tools[case["tool"]].fn(**ordinary_arguments))
        (root / "ordinary-results.json").write_text(json.dumps(oracle))
        (root / "tool-cases.json").write_text(json.dumps(cases))
        os.chdir(root / "server")
        from genesis.knowledge import tree_index

        tree_index._INDICES_DIR = root / "shared-indices"
        for tool in tools.values():
            server.add_tool(tool)
    else:
        for name in ("knowledge_ingest_source", "knowledge_ingest_batch"):
            tool = await knowledge.mcp.get_tool(name)
            assert tool.fn.__module__ == "genesis.mcp.memory.knowledge"
            server.add_tool(tool)
    tracker = ProviderActivityTracker()
    tracker.set_db(database)
    install_instrumentation(server, tracker, service, database)
    app = await build_http_app(
        server, config=SharedHTTPConfig(service, f"http://127.0.0.1:{port}/mcp",
                                       2, 2, 8, 30, 65536),
        resolver=CallerResolver("f" * 43, {}), database=database, tracker=tracker,
        loaded_commit="a" * 40, started_at="2026-01-01T00:00:00+00:00",
        enabled=lambda: True,
    )

    async def close():
        await database.close()
        record("closed")

    runner = SharedServer(uvicorn.Config(
        app, host="127.0.0.1", port=port, proxy_headers=False, access_log=False,
        log_level="warning", lifespan="on", timeout_graceful_shutdown=.25,
    ), database=database, grace_s=.25, close_resources=close)
    try:
        await runner.serve()
    finally:
        if runner.started:
            await runner.shutdown()


asyncio.run(main())
