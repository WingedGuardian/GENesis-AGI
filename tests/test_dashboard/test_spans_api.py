"""Tests for the Traces (trace-waterfall) dashboard API.

Read-only endpoints over genesis.observability.span_reader. The route flattens
``get_trace`` server-side via the real (unmocked) ``flatten_tree`` so these tests
also lock the flatten contract the waterfall renders.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _rt(bootstrapped: bool = True):
    rt = MagicMock()
    rt.is_bootstrapped = bootstrapped
    rt.db = MagicMock() if bootstrapped else None
    return rt


# ── /api/genesis/spans/recent ─────────────────────────────────────────────


def test_recent_empty_when_not_bootstrapped(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt(bootstrapped=False)
        resp = client.get("/api/genesis/spans/recent")
        assert resp.status_code == 200
        assert resp.get_json() == {"traces": []}


def test_recent_returns_traces_with_default_limit(client):
    rows = [
        {
            "span_id": "a",
            "trace_id": "t1",
            "name": "ego.cycle",
            "kind": "operation",
            "status": "ok",
            "start_unix_us": 1,
            "duration_us": 100,
            "session_id": None,
            "span_count": 3,
        }
    ]
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.observability.span_reader.list_recent_traces",
            new=AsyncMock(return_value=rows),
        ) as mock_list,
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/spans/recent")
        assert resp.status_code == 200
        assert resp.get_json()["traces"][0]["name"] == "ego.cycle"
        assert mock_list.await_args.kwargs["limit"] == 50


def test_recent_caps_limit_at_200(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.observability.span_reader.list_recent_traces",
            new=AsyncMock(return_value=[]),
        ) as mock_list,
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/spans/recent?limit=9999")
        assert resp.status_code == 200
        assert mock_list.await_args.kwargs["limit"] == 200


def test_recent_floors_limit_at_1(client):
    # A negative limit must NOT pass through (SQLite treats LIMIT -1 as no-limit).
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.observability.span_reader.list_recent_traces",
            new=AsyncMock(return_value=[]),
        ) as mock_list,
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/spans/recent?limit=-5")
        assert resp.status_code == 200
        assert mock_list.await_args.kwargs["limit"] == 1


# ── /api/genesis/spans/trace/<trace_id> ───────────────────────────────────


def test_trace_503_when_not_bootstrapped(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt(bootstrapped=False)
        resp = client.get("/api/genesis/spans/trace/anything")
        assert resp.status_code == 503


def test_trace_unknown_returns_404(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.observability.span_reader.get_trace",
            new=AsyncMock(return_value=None),
        ),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/spans/trace/nope")
        assert resp.status_code == 404


def test_trace_returns_flattened_spans_with_depth(client):
    # Real flatten_tree runs (only get_trace is mocked) → locks the flatten
    # contract: depth-first order, depth field added, children dropped.
    trace = {
        "trace_id": "t1",
        "span_count": 2,
        "roots": [
            {
                "span_id": "r",
                "trace_id": "t1",
                "parent_span_id": None,
                "name": "ego.cycle",
                "kind": "operation",
                "status": "ok",
                "start_unix_us": 0,
                "end_unix_us": 10,
                "duration_us": 10,
                "attributes": {},
                "children": [
                    {
                        "span_id": "c",
                        "trace_id": "t1",
                        "parent_span_id": "r",
                        "name": "llm.call",
                        "kind": "llm",
                        "status": "ok",
                        "start_unix_us": 2,
                        "end_unix_us": 5,
                        "duration_us": 3,
                        "attributes": {"call_site": "x"},
                        "children": [],
                    }
                ],
            }
        ],
    }
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.observability.span_reader.get_trace",
            new=AsyncMock(return_value=trace),
        ),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/spans/trace/t1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["trace_id"] == "t1"
        assert data["span_count"] == 2
        assert [s["depth"] for s in data["spans"]] == [0, 1]
        assert data["spans"][0]["name"] == "ego.cycle"
        assert "children" not in data["spans"][0]
        assert data["spans"][1]["attributes"] == {"call_site": "x"}
