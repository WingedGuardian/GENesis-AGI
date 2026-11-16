"""The new route retains the existing request-local caller-file identity."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import pytest

from genesis.mcp.shared_paths import ingestion_input
from tests.test_mcp.test_database_target_http import ENDPOINT, TARGET_KEY, target
from tests.test_mcp.test_plan_bookmark_http import META, settled
from tests.test_mcp.test_plan_bookmark_http import bookmark_app as bookmark_app
from tests.test_mcp.test_plan_bookmark_http import bookmark_http as bookmark_http
from tests.test_mcp.test_plan_bookmark_http import database as database
from tests.test_mcp.test_request_service_tool_bodies import (
    memory_service as ordinary_memory_service,
)
from tests.test_mcp.test_shared_http_runtime import unpack


@pytest.fixture
async def memory_service(database, monkeypatch):
    async for server, memory, tracker in ordinary_memory_service.__wrapped__(database, monkeypatch):
        async def batch_probe(directory: str):
            original, options = ingestion_input("knowledge_ingest_batch", "directory", directory)
            return {"original": original, "options": options}

        batch_probe.__module__ = "genesis.mcp.memory.knowledge"
        server.tool(batch_probe, name="knowledge_ingest_batch")
        yield server, memory, tracker


@pytest.mark.parametrize("valid_hint", [True, False])
async def test_target_bound_route_preserves_caller_identity_and_resets(bookmark_http, tmp_path, valid_hint):
    runtime = bookmark_http
    actual = str(tmp_path / "caller/input")
    metadata = {**META, TARGET_KEY: target(runtime), "genesis/callerPath": {
        "argument": "directory", "original": "input", "actual": actual if valid_hint else actual + "-other",
    }}

    async def invoke(identifier, metadata):
        return await runtime.client.post(ENDPOINT,
            headers={"mcp-method": "tools/call", "mcp-name": "knowledge_ingest_batch"},
            json={"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {
                "name": "knowledge_ingest_batch", "arguments": {"directory": actual}, "_meta": metadata}})

    response = await invoke("identity", metadata)
    assert response.status_code == 200
    reply = unpack(response)
    if valid_hint:
        assert not reply["result"].get("isError")
        assert reply["result"]["structuredContent"] == {"original": "input", "options": {"read_directory": actual}}
    else:
        assert reply["result"].get("isError")
    next_reply = unpack(await invoke("without-hint", {**META, TARGET_KEY: target(runtime)}))
    assert not next_reply["result"].get("isError")
    assert next_reply["result"]["structuredContent"] == {"original": actual, "options": {}}
    assert await settled(runtime) == [] and runtime.pending.exists() and runtime.decoy.exists()
