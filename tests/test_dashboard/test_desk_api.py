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
