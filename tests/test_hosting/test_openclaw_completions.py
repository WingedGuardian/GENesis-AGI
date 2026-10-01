"""Tests for the OpenClaw /v1/chat/completions endpoint."""

from __future__ import annotations

import asyncio
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


# --- readiness: is_running(), not existence ---------------------------------
#
# These exist because a mutation run deleted the readiness check entirely and
# the suite stayed GREEN. The `app` fixture supplies MagicMock() as the event
# loop, and a MagicMock auto-creates is_running() returning a truthy mock, so
# every other test in this file satisfies the predicate by accident whatever it
# says. Each test below therefore installs a REAL asyncio loop.
#
# The body is asserted, not just the status: three different conditions answer
# 503 on this route, so the status alone cannot say which one fired — the same
# reasoning test_unconfigured_token_fails_closed_not_open records above.


def test_stopped_event_loop_returns_503(client, mock_rt):
    """Configured-but-stopped is NOT ready.

    This is the expensive state, not an obvious error: run_coroutine_threadsafe
    accepts a stopped-but-open loop and returns a PENDING future, so without
    this check the request blocks on future.result(timeout=300) for five
    minutes holding one of three concurrency slots.
    """
    import asyncio

    loop = asyncio.new_event_loop()  # created, never run
    try:
        client.application.config["GENESIS_EVENT_LOOP"] = loop
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
        assert resp.status_code == 503
        assert "event loop not running" in resp.get_data(as_text=True)
        assert not resp.is_streamed  # refused before the SSE generator
    finally:
        loop.close()


def test_closed_event_loop_returns_503(client, mock_rt):
    """Closed is the state that raises — and raises too late to set a status.

    run_coroutine_threadsafe raises RuntimeError here, but inside the streaming
    generator, after Flask has committed 200. Without this check the caller
    gets 200 plus a generic apology, which is why the fix is a pre-flight
    refusal rather than an exception handler.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    loop.close()
    client.application.config["GENESIS_EVENT_LOOP"] = loop
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 503
    assert "event loop not running" in resp.get_data(as_text=True)


def test_an_object_that_is_not_a_loop_is_not_waved_through(client, mock_rt):
    """No is_running attribute must NOT default to ready.

    An earlier version used getattr(loop, "is_running", lambda: True), which
    fails OPEN on exactly the object this gate exists to catch: every real loop
    and every MagicMock has the attribute, so the default could only ever fire
    for something that is not a loop. Pinned so it cannot come back.
    """
    class NotALoop:
        pass

    client.application.config["GENESIS_EVENT_LOOP"] = NotALoop()
    # Measure what the SERVER does, not what the test harness does. With
    # TESTING=True Flask re-raises, so asserting on the exception would pin a
    # harness property while the comment reasoned about production. Turning
    # propagation off gives the real answer: 500, logged with a traceback.
    #
    # And 500 is CORRECT here, not a bug being pinned as a feature. 503 says
    # "retry later", but an object that is not a loop in this config slot is a
    # permanent wiring error, so 503 would tell every client to hammer
    # something no retry can fix. What must never happen is the request
    # proceeding as if the loop were ready -- which is exactly what the
    # getattr default did, silently.
    client.application.config["PROPAGATE_EXCEPTIONS"] = False
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 500, "an object with no is_running was treated as ready"


def test_missing_conversation_loop_names_itself_not_the_event_loop(client, mock_rt):
    """Negative control for the split: the two 503s must stay distinguishable."""
    client.application.config.pop("OPENCLAW_CONVERSATION_LOOP", None)
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 503
    assert "ConversationLoop not available" in resp.get_data(as_text=True)


def test_multimodal_text_blocks_join_with_a_blank_line():
    """The SEPARATOR is part of the contract, not an implementation detail.

    A mutation changing "\\n\\n" to "" left the suite green: the existing test
    only asserted that both blocks appear. Concatenating without a separator
    runs the last word of one block into the first of the next, which changes
    the prompt the model receives.
    """
    from genesis.hosting.openai_messages import extract_last_user_message

    out = extract_last_user_message(
        [{"role": "user", "content": [
            {"type": "text", "text": "first"},
            {"type": "image_url", "image_url": {"url": "http://example.invalid/x.png"}},
            {"type": "text", "text": "second"},
        ]}]
    )
    assert out == "first\n\nsecond"


# --- the residual race: readiness at SUBMISSION, not just at response build ---
#
# Flask runs the response generator after the view returns, so the pre-flight
# check in the view can be arbitrarily stale by the time the coroutine is
# actually submitted. Both external reviewers found this independently. The
# harm is not the error -- it is the SLOT: a pending future held for the full
# timeout starves an endpoint with only three of them.


def test_a_wait_abandons_as_soon_as_the_loop_it_needs_stops():
    """The slot must come back when the loop dies, not when the timeout expires.

    This is the whole point of the change: future.result(timeout=300) on a
    loop that has stopped waits five minutes for a coroutine that can never
    run. Asserting the ELAPSED time is what pins it -- asserting only that it
    raises would pass against the old code too, five minutes later.
    """
    import time as _time
    from concurrent.futures import Future as _Future

    from genesis.hosting.openclaw.completions import _result_while_loop_lives

    never_completes = _Future()
    dead_loop = MagicMock()
    dead_loop.is_running.return_value = False

    # timeout=1.0, not 300: the binding is identical, but a regression then
    # fails in one second with a named TimeoutError instead of hanging until
    # CI kills the job. A test whose failure mode is a five-minute hang gets
    # reported as infrastructure flake and then deleted.
    started = _time.monotonic()
    with pytest.raises(RuntimeError, match="stopped while the request was in flight"):
        _result_while_loop_lives(never_completes, dead_loop, timeout=1.0, poll=0.01)
    elapsed = _time.monotonic() - started

    assert elapsed < 0.5, f"waited {elapsed:.2f}s for a loop that was already gone"
    assert never_completes.cancelled(), "the abandoned coroutine was left submitted"


def test_a_live_loop_still_gets_its_result():
    """Negative control: the liveness poll must not break the ordinary path."""
    from concurrent.futures import Future as _Future

    from genesis.hosting.openclaw.completions import _result_while_loop_lives

    done = _Future()
    done.set_result("the answer")
    live_loop = MagicMock()
    live_loop.is_running.return_value = True

    assert _result_while_loop_lives(done, live_loop, timeout=300, poll=0.01) == "the answer"


def test_a_loop_that_stops_between_view_and_generator_does_not_hang(client, mock_rt):
    """The gap no pre-flight can close: ready at the view, stopped by submission.

    The view's check passes, then the loop stops before Flask runs the
    generator. Without the submission-time re-check this submits against a
    stopped loop and holds a slot for the full timeout; with it the request
    fails fast and the slot is freed.
    """
    import time as _time

    loop = MagicMock()
    # Ready when the view asks, stopped by the time the generator submits.
    loop.is_running.side_effect = [True, False, False]
    client.application.config["GENESIS_EVENT_LOOP"] = loop
    # A REAL coroutine. With the default MagicMock loop, handle_message()
    # returns a mock and run_coroutine_threadsafe rejects it synchronously --
    # so nothing is ever submitted, the helper is never entered, and this test
    # passes without exercising anything it claims to. Measured: it passed with
    # the pre-check deleted, in 0.04s.
    async def _never_finishes(*_a, **_kw):
        await asyncio.sleep(3600)

    client.application.config["OPENCLAW_CONVERSATION_LOOP"].handle_message = _never_finishes

    started = _time.monotonic()
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        body = resp.get_data(as_text=True)
    elapsed = _time.monotonic() - started

    assert resp.status_code == 200, "already committed by the time the generator runs"
    assert "I encountered an error" in body
    assert elapsed < 5, f"held the slot for {elapsed:.1f}s against a stopped loop"


def test_a_stopped_loop_is_never_submitted_to(client, mock_rt):
    """The pre-check must SKIP submission, not merely survive it.

    Without this, the pre-check is untestable: the liveness poll in
    _result_while_loop_lives catches the same case one interval later, so
    deleting the pre-check leaves every other test green (observed -- a
    mutation run removing it passed). The distinguishing fact is whether the
    coroutine was handed to the loop at all, so that is what is asserted.

    It matters beyond tidiness: a submitted coroutine on a stopped loop stays
    referenced by that loop's queue, and cancelling it later is a second thing
    that has to work. Not submitting is the state with no cleanup.
    """
    loop = MagicMock()
    loop.is_running.side_effect = [True, False, False]
    client.application.config["GENESIS_EVENT_LOOP"] = loop

    with patch("genesis.runtime.GenesisRuntime") as MockRT, \
            patch("asyncio.run_coroutine_threadsafe") as submit:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        resp.get_data(as_text=True)

    submit.assert_not_called()


def test_a_coroutine_timeout_is_not_mistaken_for_a_dead_loop():
    """The coroutine's OWN TimeoutError must propagate, not start a spin.

    On 3.12 concurrent.futures.TimeoutError, asyncio.TimeoutError and builtins
    TimeoutError are the SAME object. An earlier version of the helper waited
    with future.result(timeout=slice) inside `except TimeoutError`, so a
    coroutine raising TimeoutError while the loop was ALIVE skipped the
    liveness branch, looped, and hit an already-FINISHED future that re-raised
    instantly -- 29,689 iterations in two seconds, a pegged core and a held
    slot for the whole timeout. Exactly the starvation this module is about.

    The poll COUNT is the assertion that catches it. Elapsed time alone would
    not: the spin also finishes quickly when the timeout is short.
    """
    import time as _time
    from concurrent.futures import Future as _Future

    from genesis.hosting.openclaw.completions import _result_while_loop_lives

    failed = _Future()
    failed.set_exception(TimeoutError("the coroutine's own deadline"))
    live_loop = MagicMock()
    live_loop.is_running.return_value = True

    started = _time.monotonic()
    with pytest.raises(TimeoutError, match="the coroutine's own deadline"):
        _result_while_loop_lives(failed, live_loop, timeout=2.0, poll=0.01)
    elapsed = _time.monotonic() - started

    assert elapsed < 0.5, f"took {elapsed:.2f}s to surface an exception already in hand"
    assert live_loop.is_running.call_count <= 1, (
        f"polled liveness {live_loop.is_running.call_count} times for a finished future"
    )


def test_a_result_arriving_during_shutdown_is_not_discarded():
    """cancel() returning False means the answer is already in hand.

    The future can finish between the wait expiring and the cancel attempt.
    Ignoring cancel()'s return reports an error to the caller while the real
    response sits in the future -- a wrong answer, not just a slow one.
    """
    from genesis.hosting.openclaw.completions import _result_while_loop_lives

    landed_late = MagicMock()
    landed_late.cancel.return_value = False          # already FINISHED
    landed_late.result.return_value = "the late answer"
    dying_loop = MagicMock()
    dying_loop.is_running.return_value = False

    with patch(
        "genesis.hosting.openclaw.completions.futures_wait",
        return_value=(set(), {landed_late}),
    ):
        out = _result_while_loop_lives(landed_late, dying_loop, timeout=1.0, poll=0.01)

    assert out == "the late answer"
