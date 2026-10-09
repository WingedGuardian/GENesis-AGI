"""Research schemas, closed receipts, and installed owner-to-result flow."""

import asyncio
import json
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from genesis.cc.exceptions import CCRateLimitError
from genesis.db.schema import TABLES
from genesis.peers import research_fetch
from genesis.peers.broker import BrokerRefusal, PeerBroker
from genesis.peers.digests import operation_digest
from genesis.peers.research import (
    FetchArguments,
    PeerResearch,
    SearchArguments,
    search_url,
    validate_receipt,
)
from genesis.runtime.init import peers as installation
from genesis.web.search import WebSearcher
from tests.test_peers import test_coordinator_flow as flow
from tests.test_peers.test_api import task_message
from tests.test_peers.test_runtime import (  # noqa: F401
    installed as _installed_fixture,
)
from tests.test_peers.test_runtime import (
    request,
    resolve_owner,
    start,
    wait_for,
)

installed = _installed_fixture


@pytest.mark.parametrize("unsafe", [False, True])
async def test_historical_research_receipt_replayed_through_actual_provider_resume(
    installed, unsafe
):
    from genesis.security.output_scanner import scan_outbound

    await installed.registry.grant("muse", "conversation", "allow")
    await installed.registry.grant("muse", "research", "allow")
    service = await start(installed)
    network, errors, segments = [], [], []
    saved = {}

    def response(req):
        network.append("search")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Public title",
                        "url": "https://example.com",
                        "content": "Public text",
                    }
                ]
            },
        )

    service.research.searcher = WebSearcher()
    await service.research.searcher._client.aclose()
    service.research.searcher._client = httpx.AsyncClient(transport=httpx.MockTransport(response))

    async def invoke(invocation, on_event):
        entry = json.loads(Path(invocation.mcp_config).read_text())["mcpServers"]["genesis_peer"]
        parameters = StdioServerParameters(
            command=entry["command"],
            args=entry["args"],
            cwd=invocation.working_dir,
            env={
                "HOME": str(Path.home()),
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(Path.cwd() / "src"),
            },
        )
        segments.append(next(iter(service.coordinator._bindings)))
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            answer = await client.call_tool(
                "research_search", {"query": "public query", "max_results": 5}
            )
            errors.append(answer.isError)
        if len(errors) == 1:
            assert not answer.isError
            async with installed.registry.transaction() as db:
                row = await (
                    await db.execute(
                        "SELECT id,segment_id,result_json FROM peer_operations WHERE capability='research' AND status='completed'"
                    )
                ).fetchone()
                saved.update(id=row["id"], segment_id=row["segment_id"])
                if unsafe:
                    receipt = json.loads(row["result_json"])
                    receipt["data"]["results"][0]["title"] = (
                        'token: "' + secrets.token_hex(16) + '"'
                    )
                    assert scan_outbound(json.dumps(receipt, ensure_ascii=False)).safe, (
                        "legacy serialized scanner control"
                    )
                    # Emulate an old completed row; new writes must not accept it.
                    await db.execute(
                        "UPDATE peer_operations SET result_json=? WHERE id=?",
                        (json.dumps(receipt), row["id"]),
                    )
            raise CCRateLimitError("Fixture provider interruption")
        assert answer.isError is unsafe
        await on_event(flow.StreamEvent("result"))
        return flow.CCOutput("fixture-cli", "Public final answer.", "sonnet", 0, 0, 0, 1, 0)

    installed.runtime._direct_session_runner._invoker.run_streaming = invoke
    sent = await request(installed, "POST", "/message:send", json=task_message())
    assert sent.status_code == 200
    task_id = sent.json["task"]["id"]
    identity = await installed.registry.get("muse")
    async with asyncio.timeout(30):
        while (await service.owned(identity, task_id))["state"] not in {"completed", "failed"}:
            await asyncio.sleep(0.01)
    assert errors == [False, unsafe] and len(network) == 1
    assert len(segments) == 2 and segments[0] != segments[1]
    async with installed.registry.connection() as db:
        rows = await (
            await db.execute(
                "SELECT id,segment_id,status FROM peer_operations WHERE capability='research'"
            )
        ).fetchall()
        artifacts = (await (await db.execute("SELECT COUNT(*) FROM peer_artifacts")).fetchone())[0]
    assert [tuple(row) for row in rows] == [(saved["id"], saved["segment_id"], "completed")]
    assert saved["segment_id"] == segments[0]
    assert (await service.owned(identity, task_id))["state"] == (
        "failed" if unsafe else "completed"
    )
    assert artifacts == (0 if unsafe else 1)


def test_original_research_arguments_and_receipts_cannot_hide_behind_escaping():
    unsafe = 'token: "' + secrets.token_hex(16) + '"'
    arguments = SearchArguments(query=unsafe)
    research = PeerResearch(PeerBroker(None, lambda *_: None))
    with pytest.raises(BrokerRefusal, match="operation_refused"):
        research._inputs(arguments)
    result = snapshot()
    result["arguments"] = arguments.model_dump()
    digest = operation_digest(result["operation"], result["arguments"])
    with pytest.raises(ValueError, match="Research receipt refused"):
        validate_receipt(result, digest)
    assert research._inputs(SearchArguments(query="Public query")) is None
    safe = snapshot()
    validate_receipt(safe, operation_digest(safe["operation"], safe["arguments"]))


@pytest.mark.parametrize(
    "url",
    [
        "",
        "/relative",
        "javascript:alert(1)",
        "mailto:owner@example.com",
        "https://@example.com",
        "HTTPS://@example.com",
        "https://example.com/a b",
        "https://example.com:invalid",
    ],
)
def test_search_link_syntax_refuses(url):
    with pytest.raises(BrokerRefusal):
        search_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://bücher.example/#section",
        "https://example.com:8443/path",
        "https://example.com?q=a@b",
        "https://example.com#user@host",
    ],
)
def test_search_link_is_display_only(url):
    assert search_url(url) == url


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "x" * 4097},
        {"query": "x", "max_results": True},
        {"query": "x", "max_results": 0},
        {"query": "x", "max_results": 11},
        {"query": "x", "backend": "peer"},
        {"query": "x", "headers": {}},
    ],
)
def test_search_arguments_refuse(arguments):
    with pytest.raises(ValueError):
        SearchArguments.model_validate(arguments)


def snapshot():
    return {
        "operation": "research_search",
        "arguments": {"query": "public query", "max_results": 5},
        "data": {"backend": "searxng", "results": [], "source": "external_untrusted"},
    }


@pytest.mark.parametrize("change", ["operation", "arguments", "data", "source", "extra", "digest"])
def test_closed_receipt_rejects_modified_provenance(change):
    result = snapshot()
    digest = operation_digest(result["operation"], result["arguments"])
    validate_receipt(result, digest)
    if change == "source":
        result["data"]["source"] = "trusted"
    elif change == "extra":
        result["data"]["extra"] = "unknown"
    elif change == "digest":
        digest = "0" * 64
    else:
        result[change] = {} if change != "operation" else "other_operation"
    with pytest.raises(ValueError):
        validate_receipt(result, digest)


async def test_receipt_requires_current_registered_operation(registry):
    broker = PeerBroker(registry, AsyncMock())
    research = PeerResearch(broker)
    result = snapshot()
    digest = operation_digest(result["operation"], result["arguments"])
    research.validate(result, digest)
    del broker._operations["research_search"]
    with pytest.raises(ValueError):
        research.validate(result, digest)


@pytest.mark.parametrize("operation", ["search", "fetch"])
async def test_sensitive_arguments_refused_before_network(registry, operation):
    research = PeerResearch(PeerBroker(registry, AsyncMock()))
    arguments = (
        SearchArguments(query="/etc/genesis/private")
        if operation == "search"
        else FetchArguments(url="https://example.com/etc/genesis/private")
    )
    with pytest.raises(BrokerRefusal):
        await getattr(research, operation)({}, {}, arguments)
    assert research.searcher is None


@pytest.mark.parametrize(
    "body",
    [
        {"results": None},
        {"results": [None]},
        {"results": [{"title": 2}]},
        {"results": [{"score": None}]},
        {"results": [{"content": "/etc/genesis/private"}]},
    ],
)
async def test_private_backend_malformed_or_sensitive_data_refused(body, monkeypatch):
    monkeypatch.delenv("API_KEY_BRAVE", raising=False)
    searcher = WebSearcher()
    await searcher._client.aclose()
    searcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    try:
        response = await searcher.search("public query", private_observability=True)
        assert response.error == "Private search unavailable" and not response.results
    finally:
        await searcher._client.aclose()


async def test_pending_coordinator_close_retains_search_client(registry, tmp_path, monkeypatch):
    async with registry.transaction() as db:
        await db.execute(TABLES["peer_segments"])
    runtime = SimpleNamespace(_direct_session_runner=SimpleNamespace(_peer_cleanup_holds={}))
    service = installation.PeerRuntime(runtime, registry, tmp_path)
    release = asyncio.Event()

    async def close():
        await release.wait()

    service.coordinator = SimpleNamespace(quiesce=lambda: None, _notifications={}, close=close)
    service.research = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(installation, "_SHUTDOWN_GRACE_S", 0.01)
    await service.stop()
    assert not service.closing.done()
    service.research.close.assert_not_awaited()
    release.set()
    await service.closing
    service.research.close.assert_awaited_once()


async def test_partial_install_retires_owned_search_client(registry, tmp_path):
    service = installation.PeerRuntime(SimpleNamespace(), registry, tmp_path)
    service.research = SimpleNamespace(close=AsyncMock())
    await service.stop()
    await service.stop()
    service.research.close.assert_awaited_once()


@pytest.mark.parametrize("operation", ["research_search", "research_fetch"])
async def test_installed_research_owner_approval_provider_resume_result(
    installed, monkeypatch, operation
):
    await installed.registry.grant("muse", "research", "ask")
    service = await start(installed)
    network = []

    def search_response(req):
        network.append("search")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Public title",
                        "url": "https://example.com",
                        "content": "Public search text",
                    }
                ]
            },
        )

    service.research.searcher = WebSearcher()
    await service.research.searcher._client.aclose()
    service.research.searcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(search_response)
    )
    monkeypatch.setattr(research_fetch, "vetted_address", AsyncMock(return_value="1.1.1.1"))

    async def fetch_response(self, req):
        network.append("fetch")
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"Public fetch text"
        )

    monkeypatch.setattr(research_fetch.PinnedTransport, "handle_async_request", fetch_response)
    completed_calls = []
    errors = []
    arguments = (
        {"query": "public query", "max_results": 5}
        if operation == "research_search"
        else {"url": "https://example.com"}
    )

    async def invoke(invocation, on_event):
        entry = json.loads(Path(invocation.mcp_config).read_text())["mcpServers"]["genesis_peer"]
        parameters = StdioServerParameters(
            command=entry["command"],
            args=entry["args"],
            cwd=invocation.working_dir,
            env={
                "HOME": str(Path.home()),
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(Path.cwd() / "src"),
            },
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            answer = await client.call_tool(operation, arguments)
            errors.append(answer.isError)
            assert not answer.isError
            repeated = await client.call_tool(operation, arguments)
            assert not repeated.isError
            assert answer.structuredContent is not None
            assert repeated.structuredContent == answer.structuredContent
            completed_calls.append(answer)
        if len(completed_calls) == 1:
            raise CCRateLimitError("Fixture provider interruption")
        await on_event(flow.StreamEvent("result"))
        return flow.CCOutput("fixture-cli", "Research fixture answer.", "sonnet", 0, 0, 0, 1, 0)

    installed.runtime._direct_session_runner._invoker.run_streaming = invoke
    sent = await request(installed, "POST", "/message:send", json=task_message())
    assert sent.status_code == 200
    task_id = sent.json["task"]["id"]
    identity = await installed.registry.get("muse")
    for count in (1, 2):

        async def notified(count=count):
            return len(installed.notifications) == count and bool(
                await service.coordinator.approvals.pending(identity)
            )

        await wait_for(notified)
        pending = await service.coordinator.approvals.pending(identity)
        await resolve_owner(installed, pending[0]["approval_id"])

    async def completed():
        return (await service.owned(identity, task_id))["state"] == "completed"

    try:
        # Two real stdio processes plus provider resumption need more than the
        # single-hold fixture's fifteen-second observation window on this host.
        async with asyncio.timeout(30):
            while not await completed():
                await asyncio.sleep(0.01)
    except TimeoutError:
        task = await service.owned(identity, task_id)
        async with installed.registry.connection() as db:
            receipts = await (await db.execute("SELECT status FROM peer_operations")).fetchall()
            parks = await (await db.execute("SELECT status FROM cc_rate_limit_parks")).fetchall()
        raise AssertionError(
            {
                "state": task["state"],
                "tool_errors": errors,
                "completed_calls": len(completed_calls),
                "network_count": len(network),
                "receipt_states": [r[0] for r in receipts],
                "park_states": [r[0] for r in parks],
                "poll_done": service.poll.done(),
            }
        ) from None
    assert len(network) == 1 and len(completed_calls) == 2
    fetched = await request(installed, "GET", "/tasks/" + task_id)
    identifier = fetched.json["artifacts"][0]["artifactId"]
    download = await request(installed, "GET", "/tasks/" + task_id + "/artifacts/" + identifier)
    assert download.status_code == 200 and download.data == b"Research fixture answer."
    await installed.registry.grant("muse", "research", "deny")
    withheld = await request(installed, "GET", "/tasks/" + task_id + "/artifacts/" + identifier)
    assert withheld.status_code in {401, 409}
