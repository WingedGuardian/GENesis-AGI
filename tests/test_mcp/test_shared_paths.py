"""Caller-file semantics through actual wrappers, stdio and shared HTTP."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import hashlib
import json
import signal
import sys
from pathlib import Path

import httpx
import pytest

from genesis.knowledge.manifest import ManifestManager
from tests.test_mcp.test_shared_cli_startup_contract import shared_install as shared_install
from tests.test_mcp.test_shared_forwarder import frame, receive, send
from tests.test_mcp.test_shared_http_runtime import unpack


@pytest.mark.parametrize("service", ["memory"])
@pytest.mark.parametrize("era", ["2026-07-28", "2025-06-18"])
async def test_actual_ingestion_keeps_caller_file_and_source_identity(tmp_path, era, shared_install):
    install = shared_install
    caller, server = tmp_path / "caller", tmp_path / "server"
    scenarios = [(tool, argument, cached) for tool, argument in [
        ("knowledge_ingest_source", "source"), ("knowledge_ingest_batch", "directory"),
    ] for cached in [False, True]]
    manifest = ManifestManager(root=tmp_path / "manifest")
    for index, (_, _, cached) in enumerate(scenarios):
        identity = f"case-{index}/note.txt"
        for folder, text in [(caller, "CALLER fixture"), (server, "SERVER collision")]:
            (folder / f"case-{index}").mkdir(parents=True)
            (folder / identity).write_text(text)
        if cached:
            extracted = manifest.save_extracted_text(identity, "CALLER fixture", "text")
            manifest.add_source(identity, source_type="text", extracted_path=extracted,
                                unit_ids=[f"prior-unit-{index}"],
                                content_hash=hashlib.sha256(b"CALLER fixture").hexdigest()[:32])
    env = dict(install.env, GENESIS_CC_SESSION="1", GENESIS_SESSION_ORIGIN="external_untrusted")
    headers = {"Authorization": "Bearer " + "f" * 43,
               "X-Genesis-Caller": json.dumps({"dispatched": True, "supervised": False,
                                              "origin": "external_untrusted"})}
    backend = frontend = None
    mismatches, receipts = [], []
    with (tmp_path / "backend.stderr").open("wb") as backend_error, \
            (tmp_path / "frontend.stderr").open("wb") as frontend_error:
        try:
            backend = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("shared_paths_backend.py")),
                str(tmp_path), str(install.port), cwd=server, env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=backend_error,
            )
            # Bound the complete readiness operation rather than each HTTP read.
            async with httpx.AsyncClient(trust_env=False, headers=headers, timeout=None) as control, asyncio.timeout(120):  # noqa: S113
                while True:
                    assert backend.returncode is None
                    try:
                        if (await control.get(f"http://127.0.0.1:{install.port}/status")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(.02)
            frontend = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("shared_forwarder_child.py")),
                "memory", str(install.port), cwd=caller, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=frontend_error,
            )
            if era == "2025-06-18":
                await send(frontend, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                    "protocolVersion": era, "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
                assert (await receive(frontend))["result"]["protocolVersion"] == era
                await send(frontend, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            for index, (tool, argument, cached) in enumerate(scenarios):
                identity = f"case-{index}/note.txt"
                event_path = tmp_path / "path-events.jsonl"
                previous = len(event_path.read_text().splitlines()) if event_path.exists() else 0
                request = frame(19 + index, "tools/call", name=tool, arguments={
                    argument: identity if argument == "source" else f"case-{index}", "project_type": "synthetic"})
                if era == "2025-06-18":
                    request["params"].pop("_meta")
                await send(frontend, request)
                reply = await receive(frontend)
                assert type(reply["id"]) is int and reply["id"] == 19 + index
                assert not reply["result"].get("isError"), reply
                payload = reply["result"]["structuredContent"]
                row = payload if argument == "source" else payload["results"][0]
                events = [json.loads(line) for line in event_path.read_text().splitlines()[previous:]]
                distilled = [item for item in events if item["event"] == "distill"]
                expected = [] if cached else [{"event": "distill", "text": "CALLER fixture", "source": identity}]
                retained = ManifestManager(root=tmp_path / "manifest").get_units_for_source(identity)
                matches = (row["source"] == identity and distilled == expected
                           and (not cached or (row["source_type"] == "cached"
                                and row["quality_flags"] == ["duplicate_source"]
                                and retained == [f"prior-unit-{index}"])))
                receipt = {"tool": tool, "cached": cached, "row": row,
                           "distilled": distilled, "retained_units": retained, "matches": matches}
                receipts.append(receipt)
                if not matches:
                    mismatches.append(receipt)
            (tmp_path / "path-receipts.json").write_text(json.dumps(receipts, indent=2))
            actual_sources = {item["source"] for item in ManifestManager(root=tmp_path / "manifest").list_sources()}
            assert actual_sources == {f"case-{index}/note.txt" for index in range(4)}
            # Exercise the public metadata seam on the actual backend. No
            # malformed/misbound hint may reach its orchestrator constructor.
            events = tmp_path / "path-events.jsonl"
            before = events.read_text()
            actual_path = str(caller / "case-0/note.txt")
            valid = {"argument": "source", "original": "case-0/note.txt", "actual": actual_path}
            hints = [None, {}, {**valid, "extra": True}, {**valid, "argument": "directory"},
                     {**valid, "original": 3}, {**valid, "actual": "/different.txt"},
                     {**valid, "original": "https://example.org/synthetic"}]
            refused = []
            async with httpx.AsyncClient(trust_env=False, headers={
                **headers, "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28", "mcp-method": "tools/call",
                "mcp-name": "knowledge_ingest_source",
            }) as control:
                for endpoint in ("/mcp", "/mcp/v1"):
                    for hint in hints + ([valid] if endpoint == "/mcp" else []):
                        request = frame("bad-hint", "tools/call", name="knowledge_ingest_source",
                                        arguments={"source": actual_path, "project_type": "synthetic"})
                        request["params"]["_meta"]["genesis/callerPath"] = hint
                        response = await control.post(f"http://127.0.0.1:{install.port}" + endpoint, json=request)
                        reply = unpack(response)
                        assert reply.get("error") or reply["result"].get("isError"), reply
                        assert events.read_text() == before
                        refused.append({"endpoint": endpoint, "status": response.status_code, "reply": reply})
                    request = frame("old-relative", "tools/call", name="knowledge_ingest_source",
                                    arguments={"source": "case-0/note.txt", "project_type": "synthetic"})
                    reply = unpack(await control.post(f"http://127.0.0.1:{install.port}" + endpoint, json=request))
                    assert reply.get("error") or reply["result"].get("isError"), reply
                    assert events.read_text() == before
            request = frame("native-conflict", "tools/call", name="knowledge_ingest_source",
                            arguments={"source": "case-0/note.txt", "project_type": "synthetic"})
            request["params"]["_meta"] = {"genesis/callerPath": valid}
            if era == "2026-07-28":
                request["params"]["_meta"].update({
                    "io.modelcontextprotocol/protocolVersion": era,
                    "io.modelcontextprotocol/clientCapabilities": {},
                })
            await send(frontend, request)
            reply = await receive(frontend)
            assert reply["id"] == "native-conflict" and "error" in reply
            assert events.read_text() == before
            # Absence of the OPTIONAL hint remains valid on an absolute call,
            # after the malformed requests and frontend refusal.
            request = frame("unhinted", "tools/call", name="knowledge_ingest_source",
                            arguments={"source": actual_path, "project_type": "synthetic"})
            if era == "2025-06-18":
                request["params"].pop("_meta")
            await send(frontend, request)
            reply = await receive(frontend)
            assert not reply["result"].get("isError"), reply
            assert reply["result"]["structuredContent"]["source"] == actual_path
            assert json.loads(events.read_text().splitlines()[-1]) == {
                "event": "distill", "text": "CALLER fixture", "source": actual_path}
            (tmp_path / "path-refusals.json").write_text(json.dumps(refused, indent=2))
            frontend.stdin.close()
            assert await asyncio.wait_for(frontend.wait(), 30) == 0
        finally:
            for child in (frontend, backend):
                if child is not None and child.returncode is None:
                    child.send_signal(signal.SIGTERM)
                    await asyncio.wait_for(child.wait(), 60)
    assert not mismatches, ("CALLER_PATH_SEMANTICS", mismatches)
