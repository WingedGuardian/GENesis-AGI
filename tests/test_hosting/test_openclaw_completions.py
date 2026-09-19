"""Tests for the OpenClaw /v1/chat/completions endpoint."""

from __future__ import annotations

import json
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.hosting.openclaw.adapter import OpenClawAdapter
from genesis.hosting.openclaw.completions import blueprint

_TOKEN = "test-openclaw-bearer-token"


@pytest.fixture(autouse=True)
def _configured_token(monkeypatch):
    """Every test runs with the bearer token configured.

    Without it the shared ``/v1/*`` check fails closed at 503 and no test below
    reaches the behaviour it is about. The unconfigured case gets its own test.
    """
    monkeypatch.setenv("GENESIS_MCP_HTTP_TOKEN", _TOKEN)


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
    """An AUTHORIZED client — the header rides every request via environ_base."""
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {_TOKEN}"
    return c


@pytest.fixture()
def anon_client(app):
    """A client that sends no Authorization header."""
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
class TestSharedParserMultipartBehaviour:
    """The parser moved to genesis.hosting.openai_messages and is now shared.

    That move CHANGED this endpoint's behaviour: it used to return at the first
    text block, and now joins every block in order. The change is right -- in
    the common [text, image, text] shape the trailing block is the instruction
    that follows the attachment -- but every pre-existing parser test here uses
    a SINGLE block, so nothing pinned the new behaviour on this side of the
    shared helper. These do.
    """

    def test_all_text_blocks_are_joined_in_order(self):
        from genesis.hosting.openclaw.completions import _extract_last_user_message

        out = _extract_last_user_message(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look at this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                        {"type": "text", "text": "what is wrong with it"},
                    ],
                },
            ]
        )
        assert out is not None
        assert out.index("look at this") < out.index("what is wrong with it")

    def test_control_a_single_block_is_unchanged(self):
        """The pre-existing shape must behave exactly as before the move."""
        from genesis.hosting.openclaw.completions import _extract_last_user_message

        out = _extract_last_user_message(
            [
                {"role": "user", "content": [{"type": "text", "text": "just one"}]},
            ]
        )
        assert out == "just one"

    def test_control_plain_string_content_is_unchanged(self):
        from genesis.hosting.openclaw.completions import _extract_last_user_message

        out = _extract_last_user_message([{"role": "user", "content": "plain"}])
        assert out == "plain"

    def test_non_text_only_content_yields_nothing(self):
        from genesis.hosting.openclaw.completions import _extract_last_user_message

        out = _extract_last_user_message(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "x"}},
                    ],
                },
            ]
        )
        assert out is None


# ── Auth ──────────────────────────────────────────────────────────────────────
#
# This route invokes CC. The dashboard session gate exempts the whole ``/v1/*``
# prefix for machine callers, so the bearer check here is the ONLY thing between
# a caller who can reach the port and a CC subprocess.


def test_missing_authorization_header_is_refused(anon_client):
    resp = anon_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 401


def test_wrong_bearer_token_is_refused(app):
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Bearer not-the-configured-token"
    resp = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 401


def test_non_bearer_authorization_scheme_is_refused(app):
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = f"Basic {_TOKEN}"
    resp = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 401


def test_unconfigured_token_fails_closed_not_open(anon_client, mock_rt, monkeypatch):
    """No token configured must REFUSE, never open the endpoint.

    Status alone cannot carry this: a READY-check failure answers 503 too, so
    asserting only the code passes even with the gate deleted (observed — a
    mutation run removing the check left this test green). The distinguishing
    fact is WHICH 503 came back, so the body is asserted, and the runtime is
    patched ready so the readiness 503 cannot be the one under test.
    """
    monkeypatch.delenv("GENESIS_MCP_HTTP_TOKEN", raising=False)
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = anon_client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 503
    assert "GENESIS_MCP_HTTP_TOKEN" in resp.get_json()["error"]


def test_unauthorized_request_never_reaches_cc(anon_client, mock_rt):
    """Auth runs BEFORE the runtime probe, the body parse and the semaphore.

    The distinguishing assertion is the LAST one: a 401 alone would also hold if
    the route refused only after spawning work, which is the failure this
    ordering exists to prevent.
    """
    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
         patch("genesis.hosting.openclaw.completions.asyncio.run_coroutine_threadsafe",
               return_value=_mock_future()) as spawn:
        MockRT.instance.return_value = mock_rt
        resp = anon_client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        resp.get_data()  # drain: a streamed body does no work until consumed
    assert resp.status_code == 401
    spawn.assert_not_called()


def test_non_ascii_authorization_header_is_401_not_500(app, monkeypatch):
    """A high byte in the header must refuse cleanly.

    WSGI decodes headers as latin-1 and `hmac.compare_digest` refuses non-ASCII
    `str`, so comparing as text raised TypeError -> HTTP 500 with a stack trace
    on every such request. Fail-closed either way; 401 is the correct refusal.
    """
    monkeypatch.setitem(app.config, "TESTING", False)
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Bearer tokén-with-é"
    resp = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 401


def test_whitespace_only_configured_token_is_treated_as_unset(anon_client, mock_rt, monkeypatch):
    """A quoted "   " in secrets.env is not a token.

    Without the strip it counts as configured, and a request presenting the
    same blank credential then PASSES the gate.
    """
    monkeypatch.setenv("GENESIS_MCP_HTTP_TOKEN", "   ")
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = anon_client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 503
    assert "GENESIS_MCP_HTTP_TOKEN" in resp.get_json()["error"]
