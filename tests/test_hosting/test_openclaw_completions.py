"""Tests for the OpenClaw /v1/chat/completions endpoint."""

from __future__ import annotations

import json
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.hosting.openclaw.adapter import OpenClawAdapter
from genesis.hosting.openclaw.completions import blueprint


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    app.config["OPENCLAW_CONVERSATION_LOOP"] = MagicMock()
    app.config["GENESIS_EVENT_LOOP"] = MagicMock()
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def mock_rt():
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.cc_invoker = MagicMock()
    rt.cc_invoker.working_dir = None
    return rt


def _mock_future(result="Hello from Genesis!"):
    """Create a Future that resolves to the given result."""
    f = Future()
    f.set_result(result)
    return f


def _parse_sse(response_data: bytes) -> list[dict]:
    """Parse SSE stream into list of decoded data payloads."""
    chunks = []
    for line in response_data.decode().splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            chunks.append(json.loads(line[6:]))
    return chunks


# ── Happy path ────────────────────────────────────────────────────────────────


def test_valid_request_returns_200(client, mock_rt):
    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
         patch("genesis.hosting.openclaw.completions.asyncio.run_coroutine_threadsafe",
               return_value=_mock_future()):
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/event-stream")


def test_response_is_valid_sse(client, mock_rt):
    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
         patch("genesis.hosting.openclaw.completions.asyncio.run_coroutine_threadsafe",
               return_value=_mock_future()):
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    data = resp.get_data()
    assert b"data: [DONE]" in data
    chunks = _parse_sse(data)
    assert len(chunks) == 2  # content chunk + final chunk
    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello from Genesis!"
    assert chunks[1]["choices"][0]["finish_reason"] == "stop"


def test_response_has_completion_id(client, mock_rt):
    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
         patch("genesis.hosting.openclaw.completions.asyncio.run_coroutine_threadsafe",
               return_value=_mock_future()):
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "ping"}]},
        )
    chunks = _parse_sse(resp.get_data())
    cid = chunks[0]["id"]
    assert cid.startswith("chatcmpl-")
    assert chunks[1]["id"] == cid


# ── ConversationLoop integration ──────────────────────────────────────────────


def test_passes_session_key_as_user_id(client, mock_rt):
    """x-openclaw-session-key is forwarded as user_id to ConversationLoop."""
    captured = {}

    def capture_call(coro, loop):
        captured["called"] = True
        coro.close()
        return _mock_future()

    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
         patch("genesis.hosting.openclaw.completions.asyncio.run_coroutine_threadsafe",
               side_effect=capture_call):
        MockRT.instance.return_value = mock_rt
        client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"X-Openclaw-Session-Key": "user-42"},
        )
    assert captured.get("called")


# ── Error cases ───────────────────────────────────────────────────────────────


def test_no_user_message_returns_400(client, mock_rt):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "assistant", "content": "hi"}]},
        )
    assert resp.status_code == 400


def test_empty_messages_returns_400(client, mock_rt):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 400


def test_not_bootstrapped_returns_503(client):
    rt = MagicMock()
    rt.is_bootstrapped = False
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 503


def test_cc_invoker_none_returns_503(client):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.cc_invoker = None
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 503


def test_no_conversation_loop_returns_503(client, mock_rt):
    """503 when ConversationLoop was not initialized."""
    client.application.config.pop("OPENCLAW_CONVERSATION_LOOP", None)
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 503


def test_cc_exception_returns_error_sse(client, mock_rt):
    err_future = Future()
    err_future.set_exception(RuntimeError("CC failed"))

    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
         patch("genesis.hosting.openclaw.completions.asyncio.run_coroutine_threadsafe",
               return_value=err_future):
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 200  # SSE always 200; error is in the stream
    data = resp.get_data()
    assert b"data: [DONE]" in data
    chunks = _parse_sse(data)
    assert "error" in chunks[0]["choices"][0]["delta"]["content"].lower()


# ── Concurrency limiter ──────────────────────────────────────────────────────


def test_concurrency_limit_rejects_excess(client, mock_rt):
    """When semaphore is exhausted, returns busy message in SSE."""
    mock_sem = MagicMock()
    mock_sem.acquire.return_value = False  # Simulate exhausted semaphore

    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
         patch("genesis.hosting.openclaw.completions._semaphore", mock_sem):
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    data = resp.get_data()
    chunks = _parse_sse(data)
    assert "busy" in chunks[0]["choices"][0]["delta"]["content"].lower()


# ── OpenClaw-specific fields ──────────────────────────────────────────────────


def test_ignores_tools_and_store_fields(client, mock_rt):
    """tools and store fields from OpenClaw are silently ignored."""
    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
         patch("genesis.hosting.openclaw.completions.asyncio.run_coroutine_threadsafe",
               return_value=_mock_future()):
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "store": False,
                "tools": [{"type": "function", "function": {"name": "search"}}],
                "max_completion_tokens": 8192,
            },
        )
    assert resp.status_code == 200


# ── Message extraction ────────────────────────────────────────────────────────


def test_extracts_last_user_message_from_history():
    from genesis.hosting.openclaw.completions import _extract_last_user_message

    messages = [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "second message"},
    ]
    assert _extract_last_user_message(messages) == "second message"


def test_extract_handles_multimodal_content():
    from genesis.hosting.openclaw.completions import _extract_last_user_message

    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "describe this image"},
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        ]},
    ]
    assert _extract_last_user_message(messages) == "describe this image"


def test_extract_skips_non_dict_items():
    from genesis.hosting.openclaw.completions import _extract_last_user_message

    messages = [None, 123, "hello", {"role": "user", "content": "valid"}]
    assert _extract_last_user_message(messages) == "valid"


# ── Blueprint registration ────────────────────────────────────────────────────


def test_adapter_registers_blueprint():
    app = Flask(__name__)
    app.config["TESTING"] = True
    OpenClawAdapter().register_blueprints(app)
    assert "openclaw_completions" in app.blueprints
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/v1/chat/completions" in rules


def test_adapter_is_idempotent():
    """Registering twice does not raise or duplicate routes."""
    app = Flask(__name__)
    adapter = OpenClawAdapter()
    adapter.register_blueprints(app)
    adapter.register_blueprints(app)
    completions_rules = [r for r in app.url_map.iter_rules() if "/v1/chat/completions" in r.rule]
    assert len(completions_rules) == 1
