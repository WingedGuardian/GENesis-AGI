"""Actual owned TCP memory calls validate the target on a retained generation."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import json
from types import SimpleNamespace

import anyio
import pytest

from tests.test_mcp.test_database_target_http import ENDPOINT, TARGET_KEY, target
from tests.test_mcp.test_plan_bookmark_peer import META, peer, settled
from tests.test_mcp.test_plan_bookmark_peer import bookmark_app as bookmark_app
from tests.test_mcp.test_plan_bookmark_peer import bookmark_tcp as bookmark_tcp
from tests.test_mcp.test_plan_bookmark_peer import database as database
from tests.test_mcp.test_plan_bookmark_peer import memory_service as memory_service


@pytest.mark.parametrize("era", ["2026-07-28", "2025-06-18"])
async def test_target_checked_on_each_real_call_with_same_generation(bookmark_tcp, era):
    runtime = bookmark_tcp
    async with anyio.create_task_group() as owners:
        remote = peer(runtime, owners)
        # Explicit backend-contract client, before ordinary frontend adoption.
        remote.url = runtime.config.origin + ENDPOINT
        try:
            if era != "2026-07-28":
                await remote.request(SimpleNamespace(request_id=0), "initialize", {
                    "protocolVersion": era, "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"}}, initialize_version=era)
                await remote.notify(None, "notifications/initialized", None)

            async def invoke(identifier, contract):
                return await remote.request(SimpleNamespace(request_id=identifier), "tools/call", {
                    "name": "procedure_recall", "arguments": {"task_description": "synthetic-task"},
                    "_meta": {**(META if era == "2026-07-28" else {}), TARGET_KEY: contract},
                })

            assert not (await invoke("valid-first", target(runtime))).get("isError")
            assert len(await settled(runtime)) == 1 and not runtime.pending.exists()
            generation = remote.current
            runtime.pending.write_text(json.dumps({"title": "Must remain on refusal"}))
            refused = await invoke("mismatched", {"version": 1, "target": "0" * 64})
            assert refused.get("isError")
            assert len(await settled(runtime)) == 1 and runtime.pending.exists()
            assert remote.current is generation
            assert not (await invoke("valid-after", target(runtime))).get("isError")
            assert len(await settled(runtime)) == 2 and not runtime.pending.exists()
            assert remote.current is generation and runtime.status_reads == 1
            assert runtime.decoy.exists()
        finally:
            await remote.close()
            owners.cancel_scope.cancel()
    requests = [path for method, path in runtime.exchanges if method == "POST" and path != "/status"]
    assert requests and set(requests) == {ENDPOINT}
    if era == "2025-06-18":
        assert ("GET", ENDPOINT) in runtime.exchanges
        assert ("DELETE", ENDPOINT) in runtime.exchanges
