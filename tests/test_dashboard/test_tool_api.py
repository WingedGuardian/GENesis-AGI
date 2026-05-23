"""Tests for the HTTP Tool API (/api/t/) endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def app():
    """Create a test Flask app with the dashboard blueprint."""
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_tool_registry():
    """Reset the tool registry cache between tests."""
    from genesis.dashboard.routes import tool_api

    tool_api._registry = None
    yield
    tool_api._registry = None


def _mock_runtime(bootstrapped: bool = True):
    """Create a mock GenesisRuntime."""
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = bootstrapped
    return mock_rt


# ── Tool listing ─────────────────────────────────────────────────────────


def test_tool_list_returns_all_registered_tools(client):
    """GET /api/t/ returns the list of available tools."""
    resp = client.get("/api/t/")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "tools" in data
    tool_names = {t["name"] for t in data["tools"]}
    assert "health_status" in tool_names
    assert "memory_recall" in tool_names
    assert "memory_store" in tool_names
    assert "knowledge_recall" in tool_names
    assert "outreach_send" in tool_names
    assert "web_fetch" in tool_names
    assert "web_search" in tool_names


def test_tool_list_includes_endpoints_and_methods(client):
    """Each tool entry has method, endpoint, and parameters."""
    resp = client.get("/api/t/")
    data = resp.get_json()
    for tool in data["tools"]:
        assert "method" in tool
        assert "endpoint" in tool
        assert "parameters" in tool
        assert tool["endpoint"] == f"/api/t/{tool['name']}"


# ── Unknown tool ─────────────────────────────────────────────────────────


def test_unknown_tool_returns_404(client):
    """POST /api/t/nonexistent returns 404 with available tools."""
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _mock_runtime()
        resp = client.post(
            "/api/t/nonexistent",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 404
        data = resp.get_json()
        assert "Unknown tool" in data["error"]
        assert "available_tools" in data


# ── Not bootstrapped ─────────────────────────────────────────────────────


def test_tool_returns_503_when_not_bootstrapped(client):
    """Tools return 503 when Genesis runtime is not bootstrapped."""
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _mock_runtime(bootstrapped=False)
        resp = client.post(
            "/api/t/health_status",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 503
        data = resp.get_json()
        assert "not bootstrapped" in data["error"]


# ── GET tool: health_status ──────────────────────────────────────────────


def test_health_status_via_get(client):
    """GET /api/t/health_status returns health snapshot."""
    mock_snapshot = {
        "cc_sessions": {"active": 2},
        "infrastructure": {"status": "healthy"},
    }

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.tool_api._build_tool_registry",
            return_value={
                "health_status": {
                    "fn": AsyncMock(return_value=mock_snapshot),
                    "method": "GET",
                },
            },
        ),
    ):
        MockRT.instance.return_value = _mock_runtime()
        resp = client.get("/api/t/health_status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["cc_sessions"]["active"] == 2


# ── POST tool: memory_recall ─────────────────────────────────────────────


def test_memory_recall_requires_query(client):
    """memory_recall returns 400 when required 'query' param is missing."""
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _mock_runtime()
        resp = client.post(
            "/api/t/memory_recall",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "query" in str(data["error"])


def test_memory_recall_with_valid_params(client):
    """memory_recall returns results when called with valid params."""
    mock_results = [
        {"memory_id": "abc-123", "preview": "test memory", "score": 0.9}
    ]

    async def fake_recall(
        query: str,
        source: str | None = None,
        limit: int = 10,
        min_activation: float = 0.0,
        compact: bool = False,
        wing: str | None = None,
        room: str | None = None,
        include_graph: bool = True,
        expand_query_terms: bool = True,
        mode: str = "auto",
        time_range: str | None = None,
        include_subsystem: bool | list[str] = False,
        only_subsystem: str | list[str] | None = None,
    ):
        return mock_results

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.tool_api._build_tool_registry",
            return_value={
                "memory_recall": {
                    "fn": fake_recall,
                    "method": "POST",
                },
            },
        ),
    ):
        MockRT.instance.return_value = _mock_runtime()
        resp = client.post(
            "/api/t/memory_recall",
            json={"query": "test", "limit": 5},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["memory_id"] == "abc-123"


# ── POST tool: outreach_send (returns JSON string) ───────────────────────


def test_outreach_send_normalizes_json_string(client):
    """outreach_send returns JSON string — verify it's normalized to dict."""
    json_str_result = json.dumps({
        "status": "queued",
        "pending_id": "xyz-789",
    })

    async def fake_send(
        message: str,
        category: str,
        channel: str,
        urgency: str = "low",
        preferred_timing: str | None = None,
        salience_score: float = 0.5,
        labeled_surplus: bool = False,
    ):
        return json_str_result

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.tool_api._build_tool_registry",
            return_value={
                "outreach_send": {
                    "fn": fake_send,
                    "method": "POST",
                },
            },
        ),
    ):
        MockRT.instance.return_value = _mock_runtime()
        resp = client.post(
            "/api/t/outreach_send",
            json={
                "message": "test",
                "category": "alert",
                "channel": "telegram",
            },
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "queued"
        assert data["pending_id"] == "xyz-789"


# ── GET on POST tool returns usage info ──────────────────────────────────


def test_get_on_post_tool_returns_usage(client):
    """GET on a POST-only tool returns parameter docs instead of 405."""
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _mock_runtime()
        resp = client.get("/api/t/memory_recall")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["method"] == "POST"
        assert "parameters" in data
        assert "query" in data["parameters"]


# ── Filters unknown params ───────────────────────────────────────────────


def test_extra_params_are_filtered(client):
    """Extra params not in the function signature are silently dropped."""
    async def fake_search(query: str, backend: str = "auto", max_results: int = 10):
        return {"results": [], "query": query}

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.tool_api._build_tool_registry",
            return_value={
                "web_search": {
                    "fn": fake_search,
                    "method": "POST",
                },
            },
        ),
    ):
        MockRT.instance.return_value = _mock_runtime()
        resp = client.post(
            "/api/t/web_search",
            json={"query": "test", "bogus_param": "ignored"},
            content_type="application/json",
        )
        assert resp.status_code == 200


# ── Tool execution error ─────────────────────────────────────────────────


def test_tool_error_returns_500(client):
    """If a tool raises an exception, return 500 with error message."""
    async def failing_tool():
        raise RuntimeError("database gone")

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.tool_api._build_tool_registry",
            return_value={
                "health_status": {
                    "fn": failing_tool,
                    "method": "GET",
                },
            },
        ),
    ):
        MockRT.instance.return_value = _mock_runtime()
        resp = client.get("/api/t/health_status")
        assert resp.status_code == 500
        data = resp.get_json()
        assert "execution failed" in data["error"]


# ── Normalize result edge cases ──────────────────────────────────────────


def test_normalize_result_handles_plain_string(client):
    """Non-JSON string result is wrapped in a dict."""
    async def plain_string_tool():
        return "just a plain string"

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.tool_api._build_tool_registry",
            return_value={
                "test_tool": {
                    "fn": plain_string_tool,
                    "method": "GET",
                },
            },
        ),
    ):
        MockRT.instance.return_value = _mock_runtime()
        resp = client.get("/api/t/test_tool")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["result"] == "just a plain string"
