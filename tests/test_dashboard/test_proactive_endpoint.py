"""The proactive recall endpoint — /api/genesis/hook/recall.

Open (no token), returns the engine's response verbatim, and fails toward the
hook's fallback (503/500) rather than blocking the prompt. Install-agnostic:
Flask test client + mocked runtime + mocked engine (no live memory system).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def _bootstrapped_rt():
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()
    return rt


def test_happy_path_returns_engine_payload(client):
    payload = {
        "status": "ok",
        "lines": ["[Memory | 3d | voice | id:abcd1234] hi"],
        "results": [{"memory_id": "abcd1234", "collection": "episodic_memory", "kind": "memory"}],
        "procedure": None,
        "shadow": {"suppressed": 0},
        "budget": {"stance": "general", "limit": 3},
        "embedding": [0.1, 0.2],
        "timings_ms": {"total": 120.0},
        "engine": {"profile": "cc_hook"},
    }
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.memory.proactive.proactive_enabled", return_value=True),
        patch("genesis.memory.proactive.proactive_context", new=AsyncMock(return_value=payload)),
    ):
        MockRT.instance.return_value = _bootstrapped_rt()
        resp = client.post("/api/genesis/hook/recall", json={"prompt": "hello", "session_id": "s1"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["lines"] == payload["lines"]
    assert body["budget"]["stance"] == "general"
    assert body["embedding"] == [0.1, 0.2]


def test_missing_prompt_is_400(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _bootstrapped_rt()
        resp = client.post("/api/genesis/hook/recall", json={"session_id": "s1"})
    assert resp.status_code == 400


def test_blank_prompt_is_400(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _bootstrapped_rt()
        resp = client.post("/api/genesis/hook/recall", json={"prompt": "   "})
    assert resp.status_code == 400


def test_not_bootstrapped_is_503(client):
    rt = MagicMock()
    rt.is_bootstrapped = False
    rt.db = None
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = rt
        resp = client.post("/api/genesis/hook/recall", json={"prompt": "hello"})
    assert resp.status_code == 503


def test_engine_disabled_returns_clean_empty(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.memory.proactive.proactive_enabled", return_value=False),
    ):
        MockRT.instance.return_value = _bootstrapped_rt()
        resp = client.post("/api/genesis/hook/recall", json={"prompt": "hello"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "disabled"
    assert body["lines"] == []


def test_memory_not_initialized_is_503(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.memory.proactive.proactive_enabled", return_value=True),
        patch(
            "genesis.memory.proactive.proactive_context",
            new=AsyncMock(side_effect=RuntimeError("memory-mcp not initialized")),
        ),
    ):
        MockRT.instance.return_value = _bootstrapped_rt()
        resp = client.post("/api/genesis/hook/recall", json={"prompt": "hello"})
    assert resp.status_code == 503


def test_engine_error_is_500(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.memory.proactive.proactive_enabled", return_value=True),
        patch(
            "genesis.memory.proactive.proactive_context",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
    ):
        MockRT.instance.return_value = _bootstrapped_rt()
        resp = client.post("/api/genesis/hook/recall", json={"prompt": "hello"})
    assert resp.status_code == 500
