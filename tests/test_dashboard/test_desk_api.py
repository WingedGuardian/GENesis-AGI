"""Tests for POST /v1/desk/chat/completions — the desktop assistant's brain."""

from __future__ import annotations

import json
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.routes.desk_api import desk_api_bp

_TOKEN = "test-desk-bearer-token"


@pytest.fixture(autouse=True)
def _configured_token(monkeypatch):
    monkeypatch.setenv("GENESIS_MCP_HTTP_TOKEN", _TOKEN)


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(desk_api_bp)
    app.config["TESTING"] = True
    loop = MagicMock()
    loop.is_running.return_value = True
    app.config["GENESIS_EVENT_LOOP"] = loop
    return app


@pytest.fixture()
def client(app):
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {_TOKEN}"
    return c


@pytest.fixture()
def anon_client(app):
    return app.test_client()


@pytest.fixture()
def mock_rt():
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.router = MagicMock()
    return rt


def _result(content="Very good, sir.", **kw):
    r = MagicMock()
    r.success = kw.get("success", True)
    r.content = content
    r.error = kw.get("error")
    r.provider_used = kw.get("provider_used", "openrouter-deepseek-v4")
    r.model_id = kw.get("model_id", "deepseek/deepseek-v4-pro")
    r.input_tokens = kw.get("input_tokens", 11)
    r.output_tokens = kw.get("output_tokens", 4)
    return r


def _future(value):
    f = Future()
    f.set_result(value)
    return f


BODY = {
    "model": "genesis",
    "max_tokens": 400,
    "messages": [
        {"role": "system", "content": "You are a desk assistant."},
        {"role": "user", "content": "what's on today?"},
    ],
}


def _post(client, body=None, **kw):
    """POST with the runtime patched ready and the router captured."""
    captured = {}

    def run_coro(coro, loop):
        coro.close()
        return _future(kw.get("result", _result()))

    rt = kw.get("rt")
    if rt is None:
        rt = MagicMock()
        rt.is_bootstrapped = True
        rt.router = MagicMock()

    async def _noop():
        return None

    def route_call(call_site_id, messages, **kwargs):
        # Record at CALL time and hand back a real coroutine. The route submits
        # that coroutine to the loop and the harness closes it unawaited, so a
        # capture living inside the coroutine BODY would never run — which is
        # how this harness silently recorded nothing on its first pass.
        # **kwargs mirrors the REAL route_call, which takes them; a fixed
        # signature here would TypeError the moment the route passes one.
        captured["call_site_id"] = call_site_id
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return _noop()

    # Some tests hand in a runtime with NO router on purpose (the 503 path);
    # capturing must not resurrect one.
    if rt.router is not None:
        rt.router.route_call = route_call

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe",
            side_effect=run_coro,
        ),
    ):
        MockRT.instance.return_value = rt
        resp = client.post(
            "/v1/desk/chat/completions",
            json=BODY if body is None else body,
            headers=kw.get("headers"),
        )
    return resp, captured


# ── Happy path + OpenAI response shape ────────────────────────────────────────


def test_returns_openai_shaped_completion(client):
    resp, _ = _post(client)
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["object"] == "chat.completion"
    assert d["id"].startswith("chatcmpl-")
    assert d["choices"][0]["message"]["role"] == "assistant"
    assert d["choices"][0]["message"]["content"] == "Very good, sir."
    assert d["choices"][0]["finish_reason"] == "stop"


def test_reports_the_model_that_actually_answered(client):
    """Not the lane, and not what the caller asked for — the caller logs this."""
    resp, _ = _post(client)
    assert resp.get_json()["model"] == "deepseek/deepseek-v4-pro"


def test_usage_is_carried_through(client):
    resp, _ = _post(client)
    assert resp.get_json()["usage"] == {
        "prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15,
    }


def test_messages_reach_the_router_unchanged(client):
    """The caller builds its own system prompt; this endpoint must not rewrite it."""
    _, cap = _post(client)
    assert cap["messages"] == BODY["messages"]


# ── Lanes ─────────────────────────────────────────────────────────────────────


def test_no_lane_header_uses_the_desk_call_site(client):
    _, cap = _post(client)
    assert cap["call_site_id"] == "desk_primary"


def test_phone_lane_header_selects_the_fast_call_site(client):
    _, cap = _post(client, headers={"X-Genesis-Lane": "fast"})
    assert cap["call_site_id"] == "desk_fast"


def test_lane_header_is_case_and_space_insensitive(client):
    _, cap = _post(client, headers={"X-Genesis-Lane": "  FAST "})
    assert cap["call_site_id"] == "desk_fast"


def test_unknown_lane_falls_back_to_the_CAPABLE_lane(client):
    """Direction matters: silently serving an unknown lane from the FAST chain
    would degrade instruction-following with no error anywhere."""
    _, cap = _post(client, headers={"X-Genesis-Lane": "wingding"})
    assert cap["call_site_id"] == "desk_primary"


# ── Auth ──────────────────────────────────────────────────────────────────────


def test_missing_token_is_refused(anon_client):
    resp, _ = _post(anon_client)
    assert resp.status_code == 401


def test_wrong_token_is_refused(app):
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Bearer wrong"
    resp, _ = _post(c)
    assert resp.status_code == 401


def test_unauthorized_request_never_reaches_the_router(anon_client):
    """A 401 alone would also hold if the route refused AFTER routing."""
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe") as spawn,
    ):
        rt = MagicMock()
        rt.is_bootstrapped = True
        MockRT.instance.return_value = rt
        resp = anon_client.post("/v1/desk/chat/completions", json=BODY)
    assert resp.status_code == 401
    spawn.assert_not_called()


def test_unconfigured_token_fails_closed(anon_client, monkeypatch):
    monkeypatch.delenv("GENESIS_MCP_HTTP_TOKEN", raising=False)
    resp, _ = _post(anon_client)
    assert resp.status_code == 503
    assert "GENESIS_MCP_HTTP_TOKEN" in resp.get_json()["error"]["message"]


# ── Request validation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": "not a list"},
        {},
        {"messages": [{"role": "system", "content": "only a system turn"}]},
        {"messages": [{"role": "wizard", "content": "hi"}]},
        {"messages": ["not an object"]},
        {"messages": [{"role": "user", "content": {"unsupported": True}}]},
    ],
)
def test_malformed_bodies_are_400(client, body):
    resp, _ = _post(client, body=body)
    assert resp.status_code == 400


def test_text_only_block_arrays_are_flattened(client):
    """Text blocks still flatten. (An earlier version of this test also asserted
    that IMAGE blocks were flattened away — that behaviour is gone on purpose:
    see test_image_content_is_REFUSED_not_silently_dropped.)"""
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "text", "text": "at this"},
                ],
            },
        ],
    }
    resp, cap = _post(client, body=body)
    assert resp.status_code == 200
    assert cap["messages"] == [{"role": "user", "content": "look at this"}]


def test_oversized_body_is_REFUSED_not_truncated(client):
    """A body over the cap is not a value we accept — it is never trimmed to fit."""
    huge = {"messages": [{"role": "user", "content": "x" * (300 * 1024)}]}
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe") as spawn,
    ):
        MockRT.instance.return_value = MagicMock()
        resp = client.post("/v1/desk/chat/completions", json=huge)
    assert resp.status_code == 413
    spawn.assert_not_called()


# ── Failure paths ─────────────────────────────────────────────────────────────


def test_chain_exhausted_is_an_ERROR_not_an_empty_answer(client):
    """The caller would SPEAK an empty 200 as though it were a real answer."""
    resp, _ = _post(client, result=_result(None, success=False, error="all providers failed"))
    assert resp.status_code == 502
    assert "all providers failed" in resp.get_json()["error"]["message"]


def test_router_not_available_is_503(client):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.router = None
    resp, _ = _post(client, rt=rt)
    assert resp.status_code == 503


def test_not_bootstrapped_is_503(client):
    rt = MagicMock()
    rt.is_bootstrapped = False
    rt.router = MagicMock()
    resp, _ = _post(client, rt=rt)
    assert resp.status_code == 503


def test_dead_event_loop_is_503(app):
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {_TOKEN}"
    app.config["GENESIS_EVENT_LOOP"].is_running.return_value = False
    resp, _ = _post(c)
    assert resp.status_code == 503


def test_router_timeout_is_504_and_cancels(client):
    timed_out = MagicMock()
    timed_out.result.side_effect = TimeoutError()
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe",
            return_value=timed_out,
        ),
    ):
        rt = MagicMock()
        rt.is_bootstrapped = True
        rt.router = MagicMock()
        MockRT.instance.return_value = rt
        resp = client.post("/v1/desk/chat/completions", json=BODY)
    assert resp.status_code == 504
    timed_out.cancel.assert_called_once()


def test_router_exception_is_500(client):
    boom = MagicMock()
    boom.result.side_effect = RuntimeError("router blew up")
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe",
            return_value=boom,
        ),
    ):
        rt = MagicMock()
        rt.is_bootstrapped = True
        rt.router = MagicMock()
        MockRT.instance.return_value = rt
        resp = client.post("/v1/desk/chat/completions", json=BODY)
    assert resp.status_code == 500
    # The internal message must not reach the caller verbatim.
    assert "router blew up" not in resp.get_data(as_text=True)


# ── Empty completions are failures, not short answers ─────────────────────────


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t "])
def test_success_with_empty_content_is_502_not_a_silent_200(client, empty):
    """A provider CAN return success with no content — a refusal, a content
    filter, or a reasoning model that spent the whole budget thinking. The
    delegate assigns content with no None check, so it reaches here as success.

    Shipping that as a 200 makes the caller speak silence and never retry, which
    is the same failure the chain-exhausted guard exists to prevent — on the
    other branch.
    """
    resp, _ = _post(client, result=_result(empty))
    assert resp.status_code == 502
    assert "choices" not in resp.get_json()


# ── Sampling parameters actually reach the router ─────────────────────────────


def test_max_tokens_is_forwarded(client):
    """route_call forwards **kwargs to the delegate; two Genesis call sites
    already pass max_tokens this way. Dropping it leaves output length to
    whichever provider answered — which is how a turn comes back empty."""
    _, cap = _post(client, body={**BODY, "max_tokens": 1234})
    assert cap["kwargs"]["max_tokens"] == 1234


def test_max_completion_tokens_is_accepted_as_an_alias(client):
    body = {"messages": BODY["messages"], "max_completion_tokens": 777}
    _, cap = _post(client, body=body)
    assert cap["kwargs"]["max_tokens"] == 777


def test_absent_max_tokens_uses_the_default(client):
    _, cap = _post(client, body={"messages": BODY["messages"]})
    assert cap["kwargs"]["max_tokens"] == 400


def test_max_tokens_is_clamped_not_rejected(client):
    _, cap = _post(client, body={**BODY, "max_tokens": 999999})
    assert cap["kwargs"]["max_tokens"] == 8192


@pytest.mark.parametrize("bad", ["many", 0, -5, 1.5e400])
def test_invalid_max_tokens_is_400(client, bad):
    resp, _ = _post(client, body={**BODY, "max_tokens": bad})
    assert resp.status_code == 400


def test_temperature_is_forwarded_when_given(client):
    _, cap = _post(client, body={**BODY, "temperature": 0.2})
    assert cap["kwargs"]["temperature"] == 0.2


def test_temperature_is_omitted_when_absent(client):
    _, cap = _post(client)
    assert "temperature" not in cap["kwargs"]


@pytest.mark.parametrize("bad", ["hot", True, [0.5]])
def test_invalid_temperature_is_400(client, bad):
    resp, _ = _post(client, body={**BODY, "temperature": bad})
    assert resp.status_code == 400


# ── Router contract: the fallback chain must be reachable, and nothing persists ──


def test_per_attempt_timeout_leaves_room_for_the_whole_chain(client):
    """The delegate's own per-attempt default (120s) is LONGER than this
    endpoint's whole budget, so without an override one slow provider consumes
    the wall and links 2 and 3 are never tried — defeating the only reason for
    routing through the router at all."""
    _, cap = _post(client)
    per_attempt = cap["kwargs"]["timeout"]
    assert per_attempt * 3 < 110.0, "three attempts must fit inside the wall"


def test_dead_letter_is_suppressed(client):
    """A dead-letter row would persist the caller's vault-derived system prompt
    in genesis.db for 72h and re-dispatch it against a paid chain, for a reply
    that was already 502'd and nobody will read."""
    _, cap = _post(client)
    assert cap["kwargs"]["suppress_dead_letter"] is True


# ── Contract limits, stated rather than surprising ────────────────────────────


def test_streaming_is_refused_explicitly(client):
    """OpenClaw hardcodes stream:true, so a config mix-up has a real path here.
    A non-SSE body would fail the client's parser with no explanation."""
    resp, _ = _post(client, body={**BODY, "stream": True})
    assert resp.status_code == 400
    assert "stream" in resp.get_json()["error"]["message"].lower()


def test_image_content_is_REFUSED_not_silently_dropped(client):
    """Genesis's routing layer is text-only. Flattening away the image would
    answer a 'what am I looking at' turn from the text alone — a confident
    answer about something never seen, which is worse than an error."""
    body = {
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "what do you make of this?"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
            ]},
        ],
    }
    resp, cap = _post(client, body=body)
    assert resp.status_code == 400
    assert "image" in resp.get_json()["error"]["message"].lower()
    assert "call_site_id" not in cap, "must not reach the router"


def test_tool_role_is_refused_with_a_reason(client):
    resp, _ = _post(client, body={
        "messages": [{"role": "tool", "content": "result", "tool_call_id": "x"}],
    })
    assert resp.status_code == 400
    assert "tool" in resp.get_json()["error"]["message"].lower()


def test_assistant_turn_with_null_content_degrades_instead_of_400(client):
    """The ordinary shape of an assistant turn that only carried tool_calls.
    The caller sends FULL history, so one such turn must not kill an unrelated
    question."""
    body = {"messages": [
        {"role": "user", "content": "book it"},
        {"role": "assistant", "content": None},
        {"role": "user", "content": "did that work?"},
    ]}
    resp, cap = _post(client, body=body)
    assert resp.status_code == 200
    assert cap["messages"][1] == {"role": "assistant", "content": ""}


# ── The lane survives a proxy that strips unknown headers ─────────────────────


def test_model_field_selects_the_lane_when_the_header_is_absent(client):
    """A stripped header fails SILENTLY — the phone lane simply never engages
    and nothing errors. `model` is the one field every client sends."""
    _, cap = _post(client, body={**BODY, "model": "genesis-fast"})
    assert cap["call_site_id"] == "desk_fast"


def test_model_field_wins_over_a_contradicting_header(client):
    _, cap = _post(client, body={**BODY, "model": "genesis-fast"},
                   headers={"X-Genesis-Lane": "primary"})
    assert cap["call_site_id"] == "desk_fast"


def test_generic_model_still_falls_through_to_the_header(client):
    """So pointing the same config at an ordinary provider stays valid."""
    _, cap = _post(client, body={**BODY, "model": "genesis"},
                   headers={"X-Genesis-Lane": "fast"})
    assert cap["call_site_id"] == "desk_fast"


# ── Concurrency ───────────────────────────────────────────────────────────────


def test_concurrency_limit_rejects_with_503_not_a_200(client):
    """Busy must not become a 200 carrying apologetic prose — the caller would
    speak it as the answer."""
    from genesis.dashboard.routes import desk_api

    acquired = [desk_api._semaphore.acquire(timeout=1)
                for _ in range(desk_api._MAX_CONCURRENT)]
    try:
        with patch("genesis.dashboard.routes.desk_api._semaphore.acquire",
                   return_value=False):
            resp, cap = _post(client)
        assert resp.status_code == 503
        assert "choices" not in resp.get_json()
        assert "call_site_id" not in cap
    finally:
        for got in acquired:
            if got:
                desk_api._semaphore.release()


def test_semaphore_is_released_on_the_error_path(client):
    """A leak here would wedge the endpoint after _MAX_CONCURRENT failures."""
    from genesis.dashboard.routes import desk_api

    for _ in range(desk_api._MAX_CONCURRENT + 2):
        _post(client, result=_result(None))
    free = [desk_api._semaphore.acquire(blocking=False)
            for _ in range(desk_api._MAX_CONCURRENT)]
    try:
        assert all(free), "semaphore leaked a permit on the failure path"
    finally:
        for got in free:
            if got:
                desk_api._semaphore.release()


def test_chunked_body_over_the_cap_is_REFUSED(app):
    """A Content-Length-only guard is bypassed by any client that STREAMS its
    request body: content_length is None under Transfer-Encoding: chunked, so
    `or 0` compares 0 > cap and waves an arbitrarily large body through.

    Not an attacker scenario — ordinary HTTP clients chunk when the body comes
    from a generator, and werkzeug's dev server dechunks and sets
    wsgi.input_terminated, so this is the real serving path.

    This test exists because a mutation run showed the sibling Content-Length
    test staying GREEN with the materialised-length check deleted: it could not
    see this hole at all.
    """
    import io

    from werkzeug.test import EnvironBuilder, run_wsgi_app

    huge = json.dumps(
        {"messages": [{"role": "user", "content": "x" * (300 * 1024)}]}
    ).encode()
    environ = EnvironBuilder(
        path="/v1/desk/chat/completions",
        method="POST",
        input_stream=io.BytesIO(huge),
        content_type="application/json",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    ).get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input"] = io.BytesIO(huge)
    environ["wsgi.input_terminated"] = True
    environ["HTTP_TRANSFER_ENCODING"] = "chunked"

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe") as spawn,
    ):
        rt = MagicMock()
        rt.is_bootstrapped = True
        rt.router = MagicMock()
        MockRT.instance.return_value = rt
        body, status, _headers = run_wsgi_app(app.wsgi_app, environ, buffered=True)

    assert status.startswith("413"), f"got {status}: {b''.join(body)[:200]!r}"
    spawn.assert_not_called()

# ── Review round 2: the eight P2 findings ─────────────────────────────────────


class _CountingStream:
    """A WSGI input stream that reports how much was actually pulled from it.

    The point of the test below is the SIZE OF THE READ, and only a stream can
    report that — a body handed to the test client as bytes is already
    materialised before the endpoint sees it, so it could never distinguish a
    bounded read from an unbounded one.
    """

    def __init__(self, total: int):
        self.remaining = total
        self.read_bytes = 0

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        take = self.remaining if size is None or size < 0 else min(size, self.remaining)
        self.remaining -= take
        self.read_bytes += take
        return b"x" * take

    def readline(self, size=-1):
        return self.read(size)


def test_chunked_body_is_BOUNDED_not_merely_refused(client):
    """413 is not the property under test — the ALLOCATION is.

    The first version of this cap called ``request.get_data()``, which
    materialises the whole stream and only then compares its length. That
    refuses an oversized body while having already held it in memory, so the cap
    bounded nothing: on a chunked request the only live ceiling was the app-wide
    500 MiB. A test that asserted 413 passed against that code, which is why
    this one measures the read instead.
    """
    from genesis.dashboard.routes.desk_api import _MAX_BODY_BYTES

    total = 8 * 1024 * 1024
    stream = _CountingStream(total)

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe") as spawn,
    ):
        MockRT.instance.return_value = MagicMock()
        resp = client.post(
            "/v1/desk/chat/completions",
            content_type="application/json",
            environ_overrides={
                # No CONTENT_LENGTH: this is the chunked shape, where the header
                # check cannot help and the read is the only bound.
                "wsgi.input": stream,
                "CONTENT_LENGTH": "",
                "HTTP_TRANSFER_ENCODING": "chunked",
                # Werkzeug refuses to read a chunked stream unless the server
                # says it has already de-chunked it. Without this the route is
                # handed an EMPTY body and the test passes for the wrong reason
                # — which is how a bound-check quietly measures nothing.
                "wsgi.input_terminated": True,
            },
        )

    assert resp.status_code == 413
    spawn.assert_not_called()
    assert stream.read_bytes <= _MAX_BODY_BYTES * 2, (
        f"pulled {stream.read_bytes} bytes for a {_MAX_BODY_BYTES}-byte cap — "
        "the body was materialised before being measured"
    )
    assert stream.read_bytes < total, "the entire oversized body was consumed"


@pytest.mark.parametrize("body", [[], [{"role": "user"}], "a string", 42, True])
def test_non_object_json_is_400_and_says_so(client, body):
    """A list or scalar top level used to become {} and then be reported as a
    missing messages array — an accurate-sounding error about the wrong thing."""
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = MagicMock()
        resp = client.post("/v1/desk/chat/completions", json=body)
    assert resp.status_code == 400
    assert "JSON object" in resp.get_json()["error"]["message"]


@pytest.mark.parametrize(
    "block",
    [
        {"type": "text", "text": 1},  # reached str.join and raised a 500
        {"type": "text", "text": None},
        {"text": "no type at all"},  # silently DISCARDED, prompt left incomplete
        "a bare string",  # likewise
        42,
    ],
)
def test_every_content_block_is_validated_before_flattening(client, block):
    """Two of these used to be dropped in silence and one used to be a 500.

    Dropping is the worse failure: the request succeeded and routed a prompt
    that was missing material the caller sent.
    """
    payload = {"messages": [{"role": "user", "content": [block]}]}
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe") as spawn,
    ):
        MockRT.instance.return_value = MagicMock()
        resp = client.post("/v1/desk/chat/completions", json=payload)
    assert resp.status_code == 400, f"{block!r} was not refused"
    spawn.assert_not_called()


def test_a_valid_text_block_array_still_flattens(client):
    """The acceptance bar for the validation above: it must not have broken the
    shape it exists to accept."""
    resp, cap = _post(
        client,
        body={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "one"},
                        {"type": "text", "text": "two"},
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200
    assert cap["messages"][-1]["content"] == "one two"


@pytest.mark.parametrize("value", [True, False, 4.5, "4", 0.1])
def test_non_integral_token_limits_are_refused(client, value):
    """``bool`` is a subclass of ``int``, so True would otherwise arrive as 1 —
    a nonsense value silently accepted rather than refused. A fraction and a
    numeric string both survive ``int()`` too, and both mean the caller asked
    for something it did not get."""
    payload = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": value}
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe") as spawn,
    ):
        MockRT.instance.return_value = MagicMock()
        resp = client.post("/v1/desk/chat/completions", json=payload)
    assert resp.status_code == 400, f"max_tokens={value!r} was accepted"
    spawn.assert_not_called()
    # Assert the REASON, not just the refusal. False and 0.1 were already 400
    # before this fix — they fall through int() to 0 and trip the pre-existing
    # "at least 1" floor — so a status-only assertion says nothing about the
    # type guard for two of these five values.
    message = resp.get_json()["error"]["message"]
    assert "integer" in message or "whole number" in message, message


def test_a_whole_number_float_is_still_accepted(client):
    """4.0 is a token count expressed as a float, not a fractional request."""
    resp, cap = _post(
        client,
        body={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64.0},
    )
    assert resp.status_code == 200
    assert cap["kwargs"]["max_tokens"] == 64


def test_loop_closing_during_submission_is_a_structured_503(app):
    """The loop can close between the readiness check and the submission.

    ``run_coroutine_threadsafe`` then raises BEFORE the handler that turns
    router failures into structured errors, so the authenticated caller got
    Flask's unstructured 500 — and the coroutine it had just created was never
    awaited.
    """
    client = app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {_TOKEN}"
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.router = MagicMock()

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe",
            side_effect=RuntimeError("Event loop is closed"),
        ),
    ):
        MockRT.instance.return_value = rt
        resp = client.post(
            "/v1/desk/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 503
    payload = resp.get_json()
    assert "choices" not in payload, "a refusal must never carry a completion shape"
    assert payload["error"]["type"] == "server_error"
    # The other half of the fix, which the status code cannot see: the coroutine
    # was created before the submission raised, so something has to close it or
    # it is left un-awaited.
    assert rt.router.route_call.return_value.close.called, (
        "the orphaned coroutine was not closed"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"tools": [{"type": "function", "function": {"name": "f"}}]},
        {"functions": [{"name": "f"}]},
        {"tool_choice": {"type": "function", "function": {"name": "f"}}},
        {"function_call": {"name": "f"}},
    ],
)
def test_a_request_that_asks_for_a_tool_call_is_refused(client, payload):
    """Refusing is the honest answer: a text-only reply to a tool request is
    harder to diagnose than a 400.

    `tool_choice: "auto"` was removed from this list in round 4. It is OpenAI's
    DEFAULT rather than a request for anything, and refusing it contradicted the
    comment on the check itself — see
    test_an_auto_tool_choice_with_no_tools_is_ACCEPTED.
    """
    body = dict(BODY)
    body.update(payload)
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.dashboard.routes.desk_api.asyncio.run_coroutine_threadsafe") as spawn,
    ):
        MockRT.instance.return_value = MagicMock()
        resp = client.post("/v1/desk/chat/completions", json=body)
    assert resp.status_code == 400, resp.get_json()
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"tools": []},
        {"functions": []},
        {"tool_choice": "none"},
        {"function_call": "none"},
    ],
)
def test_the_no_op_tool_forms_are_ACCEPTED(client, payload):
    """These assert the OPPOSITE of a tool request.

    An empty `tools` array is a client saying it has none, and
    `tool_choice: "none"` means do not call tools. Several OpenAI-compatible
    wrappers emit them unconditionally, so refusing turns a request this
    endpoint serves perfectly into a 400 the caller cannot recover from.
    """
    body = dict(BODY)
    body.update(payload)
    resp, _ = _post(client, body=body)
    assert resp.status_code == 200, resp.get_json()


# ── Round 4: mechanisms that shipped without a test that binds them ──────────
# Each of the four below survived DELETION with the suite green. The parameter
# values that existed reached an EARLIER guard and never the one under test —
# the shape this repo calls "your own battery is the mutations you thought of".


@pytest.mark.parametrize("bad", [10**400, float("inf"), float("-inf"), float("nan")])
def test_a_temperature_that_is_numeric_but_unusable_is_400(client, bad):
    """BINDS the overflow/finiteness checks, which isinstance never reaches.

    The pre-existing negative params ("hot", True, [0.5]) are all caught by the
    isinstance test above those checks, so both of them could be deleted with
    the suite still green. These four are numeric: 10**400 raises OverflowError
    inside float(), and the three IEEE specials pass float() and fail isfinite.
    """
    resp, _ = _post(client, body={**BODY, "temperature": bad})
    assert resp.status_code == 400, resp.get_json()


@pytest.mark.parametrize("role", ["user", "system"])
def test_null_content_on_a_non_assistant_turn_is_refused(client, role):
    """BINDS the role half of the null-content guard.

    The only `content: None` case in this file is an ASSISTANT turn — the
    branch that legitimately degrades to "" for a tool-call-only message. So
    the guard could be reverted to degrading EVERY role and nothing would fail.
    """
    resp, _ = _post(
        client,
        body={"messages": [{"role": "user", "content": "hi"}, {"role": role, "content": None}]},
    )
    assert resp.status_code == 400, resp.get_json()
    assert "null" in resp.get_json()["error"]["message"]


@pytest.mark.parametrize("model", [1, True, [1], {"name": "fast"}, 2.5])
def test_a_non_string_model_does_not_500(client, model):
    """BINDS the isinstance in _lane_call_site.

    Every `model` in this file is a string, so reverting to
    `(data.get("model") or "").strip()` keeps the suite green while a truthy
    non-string 500s on .strip(). The lane must still come from the header.
    """
    resp, cap = _post(
        client, body={**BODY, "model": model}, headers={"X-Genesis-Lane": "fast"}
    )
    assert resp.status_code == 200, resp.get_json()
    assert cap["call_site_id"] == "desk_fast"


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_choice": "auto"},
        {"tools": [], "tool_choice": "auto"},
        {"function_call": "auto"},
    ],
)
def test_an_auto_tool_choice_with_no_tools_is_ACCEPTED(client, payload):
    """"auto" is OpenAI's default, and several wrappers emit it unconditionally.

    Refusing it 400s every turn such a client sends — the integration is dead on
    arrival rather than degraded. With no definitions present there is nothing
    to drop, so the choice is a no-op. Found by review: the comment above the
    check already argued for this and the code did the opposite.
    """
    resp, _ = _post(client, body={**BODY, **payload})
    assert resp.status_code == 200, resp.get_json()


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_choice": "required"},
        {"tool_choice": {"type": "function", "function": {"name": "f"}}},
        {"function_call": {"name": "f"}},
        {"tools": [{"type": "function", "function": {"name": "f"}}], "tool_choice": "none"},
    ],
)
def test_a_choice_that_demands_a_call_is_still_refused(client, payload):
    """The other side of the same boundary, so widening it did not open it.

    The last case carries DEFINITIONS: `tool_choice: "none"` forbids calling a
    tool, not knowing one exists, and this endpoint does not forward them — so
    answering anyway would silently drop context the model was meant to have.
    """
    resp, _ = _post(client, body={**BODY, **payload})
    assert resp.status_code == 400, resp.get_json()
