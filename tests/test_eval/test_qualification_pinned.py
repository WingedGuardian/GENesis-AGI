"""Original transport regressions adapted to frozen Journal v2 transitions."""

import asyncio
import dataclasses
import json
import logging
import os

import httpx
import litellm
import pytest

from genesis.eval.qualification import pinned
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_transport_fixtures import Gateway, ask, bundle, router


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


async def test_pinned_wire_request_and_answer_evidence(env):
    data, server = bundle(env), Gateway()
    callbacks = {name: list(getattr(litellm, name)) for name in ("callbacks", "success_callback")}
    with data.campaign:
        result = await ask(router(data, server), data.messages)
        assert result.success and result.content == '{"score": 1}'
        assert result.provider_used == data.alias
        answer = data.campaign.attempt("case-1")["observations"][0]
        assert answer["generation_id"] == "gen-1" and answer["usage"]["cost"] == "0.0001"
        assert [line["kind"] for line in data.campaign.lines] == [
            "manifest",
            "reservation",
            "dispatch",
            "observation",
            "billing",
        ]
        body = json.loads(server.requests[0].content)
        assert body["provider"] == {
            "only": ["Synthetic"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": 1, "completion": 2},
        }
        assert body["temperature"] == 0.0 and body["max_tokens"] == 400
        assert body["messages"] == data.messages and body["usage"] == {"include": True}
    assert callbacks == {name: list(getattr(litellm, name)) for name in callbacks}
    assert "OPENROUTER_API_KEY" not in os.environ


async def test_paid_answer_is_reused_offline_without_a_key(env, monkeypatch):
    data, server = bundle(env), Gateway()
    with data.campaign:
        await ask(router(data, server), data.messages)
    monkeypatch.delenv(pinned.KEY_ENV)
    offline = Gateway()
    with data.campaign:
        for dispatch in (True, False):
            assert (await ask(router(data, offline, dispatch=dispatch), data.messages)).content
        with pytest.raises(pinned.LocalFailure, match="unanswered"):
            await ask(router(data, offline, dispatch=False), data.messages, "case-2")
    assert not offline.requests and offline.key_reads == 0


async def test_request_cap_counts_dispatch_lines_across_restarts(env):
    # V2 replaces a caller-selected counter with a fully funded fixed schedule.
    # An interrupted dispatch also pauses replay until explicit recovery.
    data, server = bundle(env), Gateway()
    with data.campaign:
        await ask(router(data, server), data.messages)
        data.campaign.reserve("case-2")
        data.campaign.dispatch("case-2")
    with data.campaign:
        assert sum(row["dispatched"] for row in data.campaign.state.attempts.values()) == 2
        for case in ("case-1", "case-2", "case-3"):
            with pytest.raises(pinned.LocalFailure, match="stopped"):
                await ask(router(data, server), data.messages, case)
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        ("missing", "dedicated"),
        ("production_env", "production"),
        ("production_secrets", "production"),
        ("unlimited", "no credit limit"),
        ("exhausted", "exhausted"),
        ("unauthorized", "could not read"),
    ],
)
async def test_dedicated_limited_key_is_required_before_any_completion(
    env, monkeypatch, setup, match
):
    data, server = bundle(env), Gateway()
    if setup == "missing":
        monkeypatch.delenv(pinned.KEY_ENV)
    elif setup == "production_env":
        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-only-key")
    elif setup == "production_secrets":
        (env / "secrets.env").write_text("API_KEY_OPENROUTER=synthetic-only-key\n")
    elif setup == "unlimited":
        server.limit = None
    elif setup == "exhausted":
        server.remaining = 0

    def transport(request):
        return (
            httpx.Response(401, json={"error": "synthetic"})
            if setup == "unauthorized"
            else server(request)
        )

    with data.campaign:
        # Keep original parameter/node identities; financial diagnostics now
        # distinguish invalid evidence from insufficient funded credit.
        expected = {"no credit limit": "financial", "exhausted": "credit"}.get(match, match)
        with pytest.raises(pinned.LocalFailure, match=expected):
            await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
        assert [line["kind"] for line in data.campaign.lines] == ["manifest"]
    assert not server.requests


@pytest.mark.parametrize("mode", ["throttle", "timeout", "garbled"])
async def test_failed_request_is_recorded_and_trips_the_router(env, monkeypatch, mode):
    data, server = bundle(env), Gateway(status=429 if mode == "throttle" else 200)
    completions = []

    def transport(request):
        if request.method == "POST":
            completions.append(request)
            if mode == "timeout":
                raise httpx.ReadTimeout("synthetic timeout")
            if mode == "garbled":
                return httpx.Response(200, content=b"not json")
        return server(request)

    with data.campaign:
        r = router(data, server, transport=httpx.MockTransport(transport))
        for case in ("case-1", "case-2"):
            with pytest.raises(pinned.LocalFailure):
                await ask(r, data.messages, case)
        assert data.campaign.attempt("case-1")["failed"]
        assert data.campaign.state.reserved == __import__("decimal").Decimal("0.1")
    assert len(completions) == 1


async def test_cancellation_is_recorded_and_propagates(env):
    data, server = bundle(env), Gateway()

    def transport(request):
        if request.method == "POST":
            raise asyncio.CancelledError()
        return server(request)

    with data.campaign:
        with pytest.raises(asyncio.CancelledError):
            await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
        assert data.campaign.attempt("case-1")["failed"]
    assert "OPENROUTER_API_KEY" not in os.environ


@pytest.mark.parametrize("field", ["model", "provider"])
async def test_served_identity_mismatch_is_kept_but_never_scored(env, field):
    data, server = bundle(env), Gateway()

    def transport(request):
        response = server(request)
        if request.method == "POST":
            raw = json.loads(response.content)
            raw[field] = "wrong-identity"
            return httpx.Response(200, json=raw)
        return response

    with data.campaign:
        r = router(data, server, transport=httpx.MockTransport(transport))
        for case in ("case-1", "case-2"):
            with pytest.raises(pinned.LocalFailure):
                await ask(r, data.messages, case)
        assert data.campaign.attempt("case-1")["observations"]
        assert not r.served and data.campaign.state.blockers
    assert len(server.requests) == 1


async def test_provider_defaults_that_change_the_wire_request_have_zero_egress(env):
    data, server = bundle(env), Gateway()
    config = pinned.routing()
    config = dataclasses.replace(
        config,
        providers={
            **config.providers,
            data.alias: dataclasses.replace(config.providers[data.alias], params={"top_p": 0.5}),
        },
    )
    with data.campaign, pytest.raises(pinned.LocalFailure, match="implicit"):
        router(data, server, config=config)
    assert not server.requests and server.key_reads == 0


async def test_global_litellm_observers_block_before_the_key_is_read(env, monkeypatch):
    data, server = bundle(env), Gateway()
    monkeypatch.setattr(litellm, "success_callback", ["synthetic"])
    with data.campaign, pytest.raises(pinned.LocalFailure, match="observers"):
        await ask(router(data, server), data.messages)
    assert not server.requests and server.key_reads == 0


async def test_call_site_parameter_contradicting_params_is_refused(env):
    data, server = bundle(env), Gateway()
    with data.campaign:
        r = router(data, server)
        r.bind(data.contract, 1, case_id="case-1")
        with pytest.raises(pinned.LocalFailure, match="contradict"):
            await r.route_call("judge", data.messages, temperature=0.5)
        with pytest.raises(pinned.LocalFailure, match="rotation"):
            await ask(r, data.messages, chain_offset=1)
    assert not server.requests and server.key_reads == 0


@pytest.mark.parametrize(
    "change",
    [
        {"upstream": ""},
        {"max_price": {"prompt": 1}},
        {"max_price": {"prompt": True, "completion": 1}},
        {"judge": {"temperature": 0.0}},
        {"judge": {"max_tokens": True}},
        {"relevance": {"max_tokens": 10, "temperature": 2.5}},
        {"novelty": {"max_tokens": 10, "top_p": 1.1}},
        {"novelty": {"max_tokens": 10, "seed": 1.5}},
        {"novelty": {"max_tokens": 10, "reasoning": {"max_tokens": -1}}},
        {"novelty": {"max_tokens": 10, "provider": {"only": ["x"]}}},
        {"extra": 1},
    ],
)
def test_invalid_params_rejected(change, tmp_path):
    with pytest.raises(Incomplete):
        pinned.validate_params({**bundle(tmp_path).params, **change})


@pytest.mark.parametrize("alias", ["deepseek-chat", "no-such-alias"])
def test_only_openrouter_aliases_resolve(alias):
    with pytest.raises(Incomplete):
        pinned.resolve(alias)


async def test_credentials_are_redacted_from_answers_and_logs(env, caplog):
    data, server = bundle(env), Gateway(answer="echo synthetic-only-key")
    factory = logging.getLogRecordFactory()
    with data.campaign:
        result = await ask(router(data, server), data.messages)
        assert "synthetic-only-key" not in result.content and "[REDACTED]" in result.content
    assert "synthetic-only-key" not in (data.campaign.directory / "answers.jsonl").read_text()
    with caplog.at_level(logging.WARNING), pinned.private_logs():
        logging.getLogger("synthetic").warning("echo %s", "synthetic-only-key")
    assert "synthetic-only-key" not in caplog.text and "[REDACTED]" in caplog.text
    assert logging.getLogRecordFactory() is factory


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("stage", ["delegate", "cleanup"])
async def test_answer_observed_before_a_late_failure_is_kept(env, monkeypatch, cancelled, stage):
    data, server = bundle(env), Gateway()
    owner, method = (
        (pinned.LiteLLMDelegate, "call")
        if stage == "delegate"
        else (pinned.ObservedHTTPHandler, "close")
    )
    original = getattr(owner, method)

    async def fail_after(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("synthetic late failure")

    monkeypatch.setattr(owner, method, fail_after)
    with data.campaign:
        with pytest.raises(asyncio.CancelledError if cancelled else pinned.LocalFailure):
            await ask(router(data, server), data.messages)
        assert data.campaign.attempt("case-1")["observations"][0]["content"]
    with data.campaign:
        offline = router(data, server, dispatch=False)
        with pytest.raises(pinned.LocalFailure, match="stopped"):
            await ask(offline, data.messages)
        await pinned.reconcile(data.campaign, "case-1", transport=server.transport)
        data.campaign.acknowledge("failure:case-1", "synthetic reviewed late failure; replay only")
        assert (await ask(offline, data.messages)).content
    assert len(server.requests) == 1


async def test_client_retry_after_a_dropped_connection_never_sends_twice(env):
    data, server = bundle(env), Gateway()
    completions = []

    def transport(request):
        if request.method == "POST":
            completions.append(request)
            raise httpx.RemoteProtocolError("synthetic dropped connection")
        return server(request)

    with data.campaign, pytest.raises(pinned.LocalFailure):
        await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
    assert len(completions) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "synthetic upstream error", "code": 502}},
        {"id": "gen-1", "model": "xiaomi/mimo-v2.6-pro", "choices": []},
        {
            "id": "gen-1",
            "model": "xiaomi/mimo-v2.6-pro",
            "choices": [{"index": 0, "finish_reason": "error", "message": {"content": ""}}],
        },
    ],
)
async def test_http_200_error_bodies_are_failures_not_answers(env, body):
    data, server = bundle(env), Gateway()

    def transport(request):
        return httpx.Response(200, json=body) if request.method == "POST" else server(request)

    with data.campaign, pytest.raises(pinned.LocalFailure):
        await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
    with data.campaign:
        assert data.campaign.attempt("case-1")["failed"]
        assert data.campaign.state.blockers


async def test_answer_records_finish_reason(env):
    data, server = bundle(env), Gateway()
    with data.campaign:
        await ask(router(data, server), data.messages)
        assert data.campaign.attempt("case-1")["observations"][0]["finish_reason"] == "stop"


async def test_unreadable_secrets_file_refuses_before_any_request(env, monkeypatch):
    import dotenv

    def unreadable(_path):
        raise PermissionError("synthetic")

    monkeypatch.setattr(dotenv, "dotenv_values", unreadable)
    data, server = bundle(env), Gateway()
    with data.campaign, pytest.raises(pinned.LocalFailure, match="secrets.env"):
        await ask(router(data, server), data.messages)
    assert not server.requests and server.key_reads == 0
