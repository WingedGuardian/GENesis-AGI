"""Agent-to-agent connector (``/v1/agent/*``) — auth, attribution, trust stamp.

Three properties are load-bearing here and each has a negative control, so a
green cell cannot pass by the check being dead:

1. FAIL-CLOSED. With no token configured the routes are OFF (503), not "open
   on a trusted network".
2. CALLER ATTRIBUTION SURVIVES THE PROXY. ``tailscale serve`` proxies to
   127.0.0.1, so ``remote_addr`` is always localhost and can never identify
   the caller. Identity must come from ``X-Forwarded-For``.
3. THE AGENT CHANNEL IS NOT OWNER-ATTENDED. An external agent must never be
   mistaken for the owner at the message boundary.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.routes import agent_api
from genesis.dashboard.routes.agent_api import agent_api_bp

TOKEN = "test-agent-token-abc123456"  # >= MIN_TOKEN_CHARS
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(agent_api_bp)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """The limiter is module-level; a test that exhausts it must not leak."""
    yield
    agent_api._semaphore = __import__("threading").Semaphore(agent_api._MAX_CONCURRENT)


def _with_token():
    return patch.dict(os.environ, {"GENESIS_AGENT_API_TOKEN": TOKEN})


def _no_token():
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_AGENT_API_TOKEN"}
    return patch.dict(os.environ, env, clear=True)


# ── 1. auth, both directions ───────────────────────────────────────────────

@pytest.mark.parametrize("path,method", [
    ("/v1/agent/ping", "get"),
    ("/v1/agent/chat/completions", "post"),
])
def test_fail_closed_when_token_unconfigured(client, path, method):
    with _no_token():
        resp = getattr(client, method)(path, headers=AUTH)
    assert resp.status_code == 503
    assert "not configured" in resp.get_json()["error"]


@pytest.mark.parametrize("path,method", [
    ("/v1/agent/ping", "get"),
    ("/v1/agent/chat/completions", "post"),
])
def test_wrong_token_rejected(client, path, method):
    with _with_token():
        resp = getattr(client, method)(path, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_missing_authorization_header_rejected(client):
    with _with_token():
        resp = client.get("/v1/agent/ping")
    assert resp.status_code == 401


def test_non_bearer_scheme_rejected(client):
    with _with_token():
        resp = client.get("/v1/agent/ping", headers={"Authorization": f"Basic {TOKEN}"})
    assert resp.status_code == 401


def test_positive_control_valid_token_is_accepted(client):
    """If this fails, every 401/503 above proves nothing."""
    with _with_token():
        resp = client.get("/v1/agent/ping", headers=AUTH)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# ── 2. caller attribution survives the serve proxy ─────────────────────────

def test_caller_comes_from_forwarded_header_not_socket(client):
    """The whole point: behind serve the socket peer is useless."""
    with _with_token():
        resp = client.get(
            "/v1/agent/ping",
            headers={**AUTH, "X-Forwarded-For": "198.51.100.11"},
        )
    assert resp.status_code == 200
    assert resp.get_json()["caller"] == "198.51.100.11"


def test_forwarded_chain_takes_the_originating_hop(client):
    with _with_token():
        resp = client.get(
            "/v1/agent/ping",
            headers={**AUTH, "X-Forwarded-For": "198.51.100.11, 203.0.113.2"},
        )
    assert resp.get_json()["caller"] == "198.51.100.11"


def test_caller_falls_back_to_remote_addr_without_header(client):
    with _with_token():
        resp = client.get("/v1/agent/ping", headers=AUTH)
    assert resp.get_json()["caller"] == "127.0.0.1"


# ── 3. the trust stamp, with its control ───────────────────────────────────

def test_agent_channel_is_not_owner_attended():
    from genesis.cc.types import ChannelType, is_owner_attended_channel
    assert is_owner_attended_channel(ChannelType.AGENT) is False


def test_positive_control_terminal_is_owner_attended():
    """Negative control for the assertion above — the predicate is alive."""
    from genesis.cc.types import ChannelType, is_owner_attended_channel
    assert is_owner_attended_channel(ChannelType.TERMINAL) is True


# ── completions behaviour ──────────────────────────────────────────────────

def test_empty_body_is_rejected(client):
    with _with_token():
        resp = client.post(
            "/v1/agent/chat/completions", headers=AUTH, json={"messages": []},
        )
    assert resp.status_code == 400


def test_whitespace_only_message_is_rejected(client):
    with _with_token():
        resp = client.post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "   "}]},
        )
    assert resp.status_code == 400


def test_503_when_runtime_not_ready(client):
    with _with_token():
        resp = client.post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 503
    assert "starting" in resp.get_json()["error"]


def _ready_app():
    app = Flask(__name__)
    app.register_blueprint(agent_api_bp)
    app.config["TESTING"] = True
    app.config["GENESIS_CONVERSATION_LOOP"] = MagicMock()
    app.config["GENESIS_EVENT_LOOP"] = MagicMock()
    return app


def test_happy_path_returns_answer_in_openai_shape():
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "the answer"
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "what is up"}]},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["choices"][0]["message"]["content"] == "the answer"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["object"] == "chat.completion"


def test_last_user_message_wins():
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut) as rct:
        app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"},
            ]},
        )
    loop = app.config["GENESIS_CONVERSATION_LOOP"]
    loop.handle_message.assert_called_once()
    assert loop.handle_message.call_args.args[0] == "second"
    assert rct.call_count == 1


def test_request_is_stamped_with_the_agent_channel():
    """The trust boundary is set at the call site, not left to a default."""
    from genesis.cc.types import ChannelType
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        app.test_client().post(
            "/v1/agent/chat/completions",
            headers={**AUTH, "X-Forwarded-For": "198.51.100.11"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    kwargs = app.config["GENESIS_CONVERSATION_LOOP"].handle_message.call_args.kwargs
    assert kwargs["channel"] == ChannelType.AGENT
    # The session key deliberately does NOT carry the attribution value --
    # see test_session_key_ignores_attribution_headers for why.
    assert kwargs["user_id"] == "agent:default"


def test_timeout_returns_504_and_releases_the_limiter():
    app = _ready_app()
    fut = MagicMock()
    fut.result.side_effect = TimeoutError()
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 504
    # The limiter must not leak on the error path, or the endpoint wedges.
    assert agent_api._semaphore.acquire(blocking=False) is True
    agent_api._semaphore.release()


def test_invocation_failure_returns_500_and_releases_the_limiter():
    app = _ready_app()
    fut = MagicMock()
    fut.result.side_effect = RuntimeError("boom")
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 500
    assert agent_api._semaphore.acquire(blocking=False) is True
    agent_api._semaphore.release()


def test_at_capacity_returns_429():
    app = _ready_app()
    for _ in range(agent_api._MAX_CONCURRENT):
        assert agent_api._semaphore.acquire(blocking=False) is True
    with _with_token():
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 429


# ── fixes from the adversarial audit, each pinned ──────────────────────────

@pytest.mark.parametrize("bad", ["", "   ", "x", "short"])
def test_blankish_or_short_token_is_treated_as_unset(client, bad):
    """A trailing space in an env file is not a credential (finding 6)."""
    with patch.dict(os.environ, {"GENESIS_AGENT_API_TOKEN": bad}):
        resp = client.get("/v1/agent/ping", headers={"Authorization": f"Bearer {bad}"})
    assert resp.status_code == 503


def test_non_ascii_authorization_is_401_not_500(client):
    """compare_digest raises TypeError on non-ASCII str; werkzeug decodes
    header bytes as latin-1, so this is reachable over the wire (finding 5)."""
    with _with_token():
        resp = client.get(
            "/v1/agent/ping", headers={"Authorization": "Bearer \xff\xfe"},
        )
    assert resp.status_code == 401


def test_bearer_scheme_is_case_insensitive(client):
    """RFC 7235 2.1: auth-scheme is a case-insensitive token (finding 15)."""
    with _with_token():
        resp = client.get("/v1/agent/ping", headers={"Authorization": f"bearer {TOKEN}"})
    assert resp.status_code == 200


def test_tailscale_user_header_beats_forwarded_for(client):
    """serve clears Tailscale-User-* before setting them; XFF is the fallback."""
    with _with_token():
        resp = client.get("/v1/agent/ping", headers={
            **AUTH,
            "Tailscale-User-Login": "someone@example.com",
            "X-Forwarded-For": "198.51.100.11",
        })
    assert resp.get_json()["caller"] == "someone@example.com"


def test_caller_cannot_inject_a_log_line(client):
    with _with_token():
        resp = client.get("/v1/agent/ping", headers={
            **AUTH, "X-Forwarded-For": "198.51.100.11 fake-line-here",
        })
    caller = resp.get_json()["caller"]
    assert "\n" not in caller and "\r" not in caller
    assert len(caller) <= 64


def test_multimodal_content_array_is_parsed_not_stringified():
    """An off-the-shelf OpenAI client sends content arrays (finding 3)."""
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": [
                {"type": "text", "text": "hello there"},
            ]}]},
        )
    forwarded = app.config["GENESIS_CONVERSATION_LOOP"].handle_message.call_args.args[0]
    assert forwarded == "hello there"


def test_trailing_empty_message_does_not_mask_the_real_question():
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [
                {"role": "user", "content": "real question"},
                {"role": "user", "content": ""},
            ]},
        )
    assert resp.status_code == 200
    assert app.config["GENESIS_CONVERSATION_LOOP"].handle_message.call_args.args[0] == "real question"


def test_slash_intents_in_external_prose_are_never_scanned():
    """The external caller must not reach the /model and /effort levers
    (finding 4). intent_text="" is what withholds them."""
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user",
                                "content": "tell me a joke /model fable /effort high"}]},
        )
    kwargs = app.config["GENESIS_CONVERSATION_LOOP"].handle_message.call_args.kwargs
    assert kwargs["intent_text"] == ""


def test_stream_true_is_refused_not_ignored():
    app = _ready_app()
    with _with_token():
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    assert resp.status_code == 400
    assert "streaming is not supported" in resp.get_json()["error"]


def test_oversized_message_is_refused_before_cognition():
    app = _ready_app()
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe") as rct:
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user",
                                "content": "x" * (agent_api._MAX_MESSAGE_CHARS + 1)}]},
        )
    assert resp.status_code == 413
    rct.assert_not_called()


def test_a_message_at_the_cap_is_still_accepted():
    """Boundary control for the test above — the cap must not be off by one."""
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user",
                                "content": "x" * agent_api._MAX_MESSAGE_CHARS}]},
        )
    assert resp.status_code == 200


def test_timeout_cancels_the_inflight_coroutine():
    """Releasing the slot without cancelling defeats the cap (finding 7)."""
    app = _ready_app()
    fut = MagicMock()
    fut.result.side_effect = TimeoutError()
    with _with_token(), patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut):
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 504
    fut.cancel.assert_called_once()


def test_long_answer_is_not_split_for_the_agent_channel():
    """An HTTP endpoint must not inherit a messaging-app length limit
    (finding 2). Round-trip identity, not merely arrival."""
    from genesis.cc.formatter import ResponseFormatter
    from genesis.cc.types import ChannelType
    text = "A" * 3000 + "\n\n" + "B" * 3000
    assert ResponseFormatter().format(text, channel=ChannelType.AGENT) == [text]


def test_positive_control_whatsapp_is_still_split():
    """If the splitter stopped splitting entirely, the test above would pass
    for the wrong reason."""
    from genesis.cc.formatter import ResponseFormatter
    from genesis.cc.types import ChannelType
    text = "A" * 3000 + "\n\n" + "B" * 3000
    assert len(ResponseFormatter().format(text, channel=ChannelType.WHATSAPP)) > 1


def test_agent_channel_is_enumerated_for_steering_origin():
    """An unenumerated channel fires a WARNING written to read as an attack
    signal, at every routine turn (finding 14)."""
    from genesis.learning.pipeline import _CHANNEL_ORIGIN
    assert _CHANNEL_ORIGIN.get("agent") == "external_untrusted"


def test_agent_is_not_granted_owner_origin():
    """Control for the above: enumerating it must not GRANT anything."""
    from genesis.learning.pipeline import _CHANNEL_ORIGIN
    assert _CHANNEL_ORIGIN.get("agent") != "owner"
# ── Cross-model review findings, each pinned with a control ────────────────


def test_session_key_ignores_attribution_headers():
    """The conversation key must NOT derive from an unverified header.

    Otherwise two unrelated chats from one agent silently share persisted
    context, and anything reaching the port directly selects a stored session
    by forging a header. The module claims attribution is logging-only; this
    is what makes that claim true.
    """
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with (
        _with_token(),
        patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut),
    ):
        app.test_client().post(
            "/v1/agent/chat/completions",
            headers={
                **AUTH,
                "X-Forwarded-For": "198.51.100.11",
                "Tailscale-User-Login": "someone@example.com",
            },
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    kwargs = app.config["GENESIS_CONVERSATION_LOOP"].handle_message.call_args.kwargs
    assert kwargs["user_id"] == "agent:default"
    assert "198.51.100.11" not in kwargs["user_id"]
    assert "someone@example.com" not in kwargs["user_id"]


def test_caller_named_conversation_selects_a_distinct_session():
    """Control for the above: the caller CAN still separate its threads, by
    naming one deliberately rather than having one inferred."""
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with (
        _with_token(),
        patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut),
    ):
        app.test_client().post(
            "/v1/agent/chat/completions",
            headers={**AUTH, "X-Genesis-Conversation-Id": "errand-7"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    kwargs = app.config["GENESIS_CONVERSATION_LOOP"].handle_message.call_args.kwargs
    assert kwargs["user_id"] == "agent:errand-7"


def test_conversation_id_is_sanitised_and_bounded():
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with (
        _with_token(),
        patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut),
    ):
        app.test_client().post(
            "/v1/agent/chat/completions",
            headers={**AUTH, "X-Genesis-Conversation-Id": "a/b c;" + "z" * 200},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    uid = app.config["GENESIS_CONVERSATION_LOOP"].handle_message.call_args.kwargs["user_id"]
    key = uid.split(":", 1)[1]
    assert len(key) <= 64
    assert "/" not in key and " " not in key and ";" not in key


@pytest.mark.parametrize("body", ["[1]", '"text"', "42", "null"])
def test_non_object_json_is_a_client_error_not_a_500(client, body):
    """Valid JSON that is not a mapping must not reach .get and raise."""
    with _with_token():
        resp = client.post(
            "/v1/agent/chat/completions",
            headers={**AUTH, "Content-Type": "application/json"},
            data=body,
        )
    assert resp.status_code == 400
    assert resp.status_code != 500


def test_malformed_json_is_a_client_error(client):
    with _with_token():
        resp = client.post(
            "/v1/agent/chat/completions",
            headers={**AUTH, "Content-Type": "application/json"},
            data="{not json",
        )
    assert resp.status_code == 400


def test_oversized_body_is_refused_before_parsing(client):
    """The message-length check runs far too late: the body is buffered and
    parsed first, and the app-wide limit is 500 MB."""
    huge = (
        b'{"messages":[{"role":"user","content":"'
        + b"x" * (agent_api._MAX_BODY_BYTES + 10)
        + b'"}]}'
    )
    with _with_token():
        resp = client.post(
            "/v1/agent/chat/completions",
            headers={**AUTH, "Content-Type": "application/json"},
            data=huge,
        )
    assert resp.status_code == 413


def test_control_a_normal_body_is_still_accepted():
    """Without this, a cap that rejected everything would pass above."""
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with (
        _with_token(),
        patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut),
    ):
        resp = app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "an ordinary question"}]},
        )
    assert resp.status_code == 200


def test_every_text_block_of_a_multipart_message_is_forwarded():
    """[text, image, text] -- the trailing block is usually the instruction
    that follows an attachment, and returning at the first one drops it."""
    app = _ready_app()
    fut = MagicMock()
    fut.result.return_value = "ok"
    with (
        _with_token(),
        patch.object(agent_api.asyncio, "run_coroutine_threadsafe", return_value=fut),
    ):
        app.test_client().post(
            "/v1/agent/chat/completions",
            headers=AUTH,
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look at this"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                            {"type": "text", "text": "what is wrong with it"},
                        ],
                    }
                ]
            },
        )
    forwarded = app.config["GENESIS_CONVERSATION_LOOP"].handle_message.call_args.args[0]
    assert "look at this" in forwarded
    assert "what is wrong with it" in forwarded
