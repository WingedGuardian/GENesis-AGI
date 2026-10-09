"""Offline wire/evidence tests; no real credential or provider is consulted."""

import asyncio
import hashlib
import json
import logging
from decimal import Decimal, localcontext

import httpx
import pytest

from genesis.eval.qualification import pinned
from genesis.eval.qualification.accounting import Journal
from genesis.eval.qualification.evidence import Incomplete, digest
from genesis.eval.qualification.pinned import (
    ENDPOINT,
    MAX_CURRENCY_CHARS,
    LocalFailure,
    ObservedHTTPHandler,
    financial_json,
    fixed_currency,
    key_status,
    reconcile,
)
from tests.test_eval.qualification_transport_fixtures import (
    Gateway,
    ask,
    bundle,
    delegate_campaign,
    router,
    routing_metadata,
)


@pytest.mark.parametrize(
    ("lexeme", "expected"),
    [
        ("0", "0"),
        ("0e-999999999", "0"),
        ("1e-4", "0.0001"),
        ("1.2300e-4", "0.00012300"),
        ("0.123456789012345678901234567890123456789", "0.123456789012345678901234567890123456789"),
    ],
)
def test_money_comes_from_lexical_bytes_without_context_rounding(lexeme, expected):
    with localcontext() as context:
        context.prec = 2
        body = financial_json(('{"usage":{"cost":' + lexeme + "}}").encode(), (("usage", "cost"),))
    assert body["usage"]["cost"] == expected


@pytest.mark.parametrize(
    "lexeme",
    ["-0", "-1", "NaN", "Infinity", "1e999999999", "1e-999999999", "true", "null", '"0.1"'],
)
def test_invalid_money_never_becomes_settlement(lexeme):
    with pytest.raises(LocalFailure):
        financial_json(('{"cost":' + lexeme + "}").encode(), (("cost",),))


@pytest.mark.parametrize(
    "body", [b'{"cost":0,"cost":1}', b'{"cost":0,"other":NaN}', b"[]", b"{}", b"garbled"]
)
def test_full_json_authority_precedes_financial_normalization(body):
    with pytest.raises(LocalFailure):
        financial_json(body, (("cost",),))


def test_representation_bound_is_checked_before_exponent_expansion():
    assert len(fixed_currency(Decimal("1e-4094"))) == MAX_CURRENCY_CHARS
    with pytest.raises(LocalFailure, match="representation limit"):
        fixed_currency(Decimal("1e-4095"))


@pytest.mark.parametrize("route_name", pinned.ROUTES)
@pytest.mark.parametrize(
    "reasoning",
    [
        {"enabled": 1},
        {"enabled": None},
        {"exclude": "false"},
        {"exclude": 1},
        {"effort": "banana"},
        {"effort": None},
        {"effort": []},
        {"max_tokens": True},
        {"max_tokens": 1.5},
        {"effort": "low", "max_tokens": 10},
        {"unknown": 0},
    ],
)
def test_reasoning_schema_refuses_unsupported_and_ambiguous_controls(
    tmp_path, route_name, reasoning
):
    _, _, params, _, _ = delegate_campaign(tmp_path)
    params[route_name]["reasoning"] = reasoning
    with pytest.raises(Incomplete, match="reasoning"):
        pinned.validate_params(params)


@pytest.mark.parametrize("route_name", pinned.ROUTES)
@pytest.mark.parametrize(
    "reasoning",
    [{"effort": level} for level in ("max", "xhigh", "high", "medium", "low", "minimal", "none")]
    + [{"max_tokens": 0}, {"enabled": False, "exclude": True}],
)
def test_reasoning_schema_acceptance_does_not_claim_backend_support(
    tmp_path, route_name, reasoning
):
    _, _, params, _, _ = delegate_campaign(tmp_path)
    params[route_name]["reasoning"] = reasoning
    assert pinned.validate_params(params)[route_name]["reasoning"] == reasoning


@pytest.mark.parametrize("kind", ["judge", "relevance", "novelty"])
async def test_production_adapter_delegate_billing_and_offline_replay(
    tmp_path, isolated_key, monkeypatch, kind
):
    from genesis.eval.qualification import contracts, corpus
    from genesis.eval.rubrics import get_rubric
    from genesis.eval.scorers import LLMJudgeScorer
    from tests.test_eval.qualification_fixtures import novelty_case, relevance_case

    campaign, alias, params, rubric, _messages = delegate_campaign(tmp_path)
    contract = {"judge": rubric, "relevance": corpus.RELEVANCE, "novelty": corpus.NOVELTY}[kind]
    case = (
        novelty_case("wire", "candidate-b") if kind == "novelty" else relevance_case("wire", True)
    )

    class Capture(contracts.Probe):
        async def route_call(self, call_site_id, messages, **kwargs):
            self.kwargs = kwargs
            return await super().route_call(call_site_id, messages, **kwargs)

    async def invoke(target):
        if kind == "judge":
            return await LLMJudgeScorer(router=target).score_async(
                "synthetic actual",
                "synthetic expected",
                {
                    "rubric_name": rubric,
                    **{key: "synthetic context" for key in get_rubric(rubric).extra_placeholders},
                },
            )
        if kind == "relevance":
            return await contracts.relevance(case, target)
        async with contracts.Sandbox(tmp_path / "adapter-storage") as sandbox:
            return await contracts.novelty(case, target, sandbox)

    probe = Capture()
    await invoke(probe)
    messages = probe.calls[0][0]
    assert set(probe.kwargs) - pinned.ROUTE_KEYS <= {"chain_offset"}
    params[kind].update(
        {key: value for key, value in probe.kwargs.items() if key in pinned.ROUTE_KEYS}
    )
    manifest = campaign.expected_manifest
    manifest["binding"]["transports"][alias]["params"] = params
    spec = manifest["attempts"][0]
    spec.update(
        contract=contract, version=corpus.versions()[contract], prompt_hash=digest(messages)
    )
    body = {
        "model": spec["model"],
        "messages": messages,
        **params[kind],
        "provider": {
            "only": ["Synthetic"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": params["max_price"],
        },
        "usage": {"include": True},
    }
    spec["request_hash"] = digest(body)
    answer = {
        "judge": '{"score": 1, "rationale": "synthetic"}',
        "relevance": '{"relevance": 1, "reason": "synthetic"}',
        "novelty": '{"redundant_with": 1}',
    }[kind]
    gateway = Gateway(answer=answer)
    with campaign:
        target = pinned.PinnedRouter(
            campaign, alias=alias, params=params, dispatch=True, transport=gateway.transport
        )
        target.bind(contract, 1, case_id="case-0")
        measured = await invoke(target)
        assert not target.local and not campaign.state.blockers
        assert campaign.state.settled == Decimal("0.0001")
        if kind == "judge":
            assert not json.loads(measured[2]).get("error")
        else:
            expected = {
                "prediction": True if kind == "relevance" else "candidate-b",
                "error": None,
            }
            if kind == "novelty":
                expected["candidate_ids"] = ["candidate-b", "candidate-a"]
            assert measured == expected
    monkeypatch.delenv(pinned.KEY_ENV)
    with campaign:
        target = pinned.PinnedRouter(
            campaign, alias=alias, params=params, dispatch=False, transport=gateway.transport
        )
        target.bind(contract, 1, case_id="case-0")
        assert await invoke(target) == measured
    assert len(gateway.requests) == 1 and gateway.key_reads == 1 and gateway.receipt_reads == 1


def wire_campaign(tmp_path):
    request = {"model": "synthetic/model", "messages": [{"role": "user", "content": "hello"}]}
    manifest = {
        "version": 2,
        "budget": "0.1",
        "binding": {"source": "synthetic-wire-test"},
        "attempts": [
            {
                "id": "case-0",
                "alias": "synthetic",
                "model": "synthetic/model",
                "upstream": "Synthetic",
                "endpoint": ENDPOINT + "/chat/completions",
                "request_hash": digest(request),
                "max_charge": "0.1",
                "generation_namespace": "openrouter",
                "receipt_provider": "Synthetic Gateway Display",
                "provider_identity_evidence": "synthetic endpoint fixture mapping",
                "credential_fingerprint": hashlib.sha256(b"synthetic-only-key").hexdigest(),
            }
        ],
    }
    return Journal(tmp_path / "campaign", manifest), request


def response_bytes(cost="0.0001"):
    return (
        '{"id":"gen-0","model":"synthetic/model",'
        '"openrouter_metadata":' + json.dumps(routing_metadata("synthetic/model", "Synthetic Gateway Display")) + ','
        '"choices":[{"message":{"content":"answer"},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":20,"completion_tokens":5,"cost":' + cost + "}}"
    ).encode()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_response_is_durable_before_auth_cleanup(tmp_path, cancelled):
    campaign, expected = wire_campaign(tmp_path)

    class CleanupFault(httpx.Auth):
        async def async_auth_flow(self, request):
            yield request
            assert campaign.attempt("case-0")["observations"]
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("synthetic auth cleanup failure")

    def record(observation):
        campaign.observe(
            "case-0",
            {
                **observation,
                "model": observation["response_model"],
                "upstream": campaign.expected_manifest["attempts"][0]["upstream"],
            },
        )

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=response_bytes()))
    with campaign:
        campaign.reserve("case-0")
        campaign.dispatch("case-0")
        handler = ObservedHTTPHandler(expected, "synthetic-key", record, transport=transport)
        try:
            with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
                await handler.client.post(
                    ENDPOINT + "/chat/completions",
                    json=expected,
                    headers={"Authorization": "Bearer synthetic-key"},
                    auth=CleanupFault(),
                )
            assert campaign.attempt("case-0")["observations"][0]["usage"]["cost"] == "0.0001"
            assert campaign.state.reserved == Decimal("0.1")
        finally:
            await handler.close()
    with Journal(campaign.directory, campaign.expected_manifest) as reopened:
        assert reopened.attempt("case-0")["observations"][0]["content"] == "answer"
        assert reopened.state.reserved == Decimal("0.1")


async def test_replacement_client_cannot_send_second_completion(tmp_path):
    campaign, expected = wire_campaign(tmp_path)
    sent = []
    transport = httpx.MockTransport(
        lambda request: (sent.append(request), httpx.Response(200, content=response_bytes()))[1]
    )
    handler = ObservedHTTPHandler(
        expected, "synthetic-key", lambda _value: None, transport=transport
    )
    replacement = handler.create_client()
    try:
        await handler.client.post(
            ENDPOINT + "/chat/completions",
            json=expected,
            headers={"Authorization": "Bearer synthetic-key"},
        )
        with pytest.raises(LocalFailure, match="repeated"):
            await replacement.post(
                ENDPOINT + "/chat/completions",
                json=expected,
                headers={"Authorization": "Bearer synthetic-key"},
            )
        assert len(sent) == 1
    finally:
        await replacement.aclose()
        await handler.close()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("partial", [False, True])
async def test_stream_close_cannot_discard_complete_durable_answer(tmp_path, cancelled, partial):
    campaign, expected = wire_campaign(tmp_path)
    sent, closes = [], []

    class FaultyStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield response_bytes()[:10] if partial else response_bytes()
            if partial:
                raise httpx.ReadError("synthetic partial read")

        async def aclose(self):
            closes.append(True)
            if not partial:
                assert campaign.attempt("case-0")["observations"]
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("synthetic stream cleanup failure")

    def transport(request):
        sent.append(request)
        return httpx.Response(200, stream=FaultyStream())

    with campaign:
        campaign.reserve("case-0")
        campaign.dispatch("case-0")
        handler = ObservedHTTPHandler(
            expected, "synthetic-key",
            lambda value: campaign.observe("case-0", value),
            transport=httpx.MockTransport(transport),
        )
        try:
            failure = httpx.ReadError if partial else (asyncio.CancelledError if cancelled else RuntimeError)
            with pytest.raises(failure):
                await handler.client.post(
                    ENDPOINT + "/chat/completions", json=expected,
                    headers={"Authorization": "Bearer synthetic-key"},
                )
            observations = campaign.attempt("case-0")["observations"]
            assert bool(observations) is not partial
            if not partial:
                assert observations[0]["content"] == "answer"
                assert observations[0]["generation_id"] == "gen-0"
            assert campaign.state.reserved == Decimal("0.1")
        finally:
            await handler.close()
    with Journal(campaign.directory, campaign.expected_manifest) as reopened:
        assert bool(reopened.attempt("case-0")["observations"]) is not partial
        assert reopened.state.reserved == Decimal("0.1")
    assert len(sent) == len(closes) == 1


@pytest.mark.parametrize("change", ["method", "url", "authorization", "model", "messages"])
async def test_wire_mismatch_refuses_before_mock_egress(change):
    expected = {"model": "synthetic/model", "messages": []}
    sent = []
    handler = ObservedHTTPHandler(
        expected,
        "synthetic-key",
        lambda _value: None,
        transport=httpx.MockTransport(lambda request: sent.append(request)),
    )
    body = {**expected}
    if change in ("model", "messages"):
        body[change] = "changed"
    request = handler.client.build_request(
        "GET" if change == "method" else "POST",
        ENDPOINT + ("/other" if change == "url" else "/chat/completions"),
        content=json.dumps(body),
        headers={
            "Authorization": "Bearer " + ("wrong" if change == "authorization" else "synthetic-key")
        },
    )
    try:
        with pytest.raises(LocalFailure, match="differs"):
            await handler.client.send(request)
        assert not sent
    finally:
        await handler.close()


@pytest.mark.parametrize("error", [None, {"code": 500, "message": "lookup failed"}])
async def test_key_error_envelope_never_authorizes_egress(error):
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"error": error, "data": {
                "limit": 1, "limit_remaining": 1, "usage": 0, "limit_reset": None,
            }},
        )
    )
    with pytest.raises(LocalFailure, match="error envelope"):
        await key_status("synthetic-key", transport)


@pytest.fixture
def isolated_key(monkeypatch, tmp_path):
    monkeypatch.setenv("GENESIS_QUALIFICATION_OPENROUTER_KEY", "synthetic-only-key")
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent-secrets.env"))
    for name in ("API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "change",
    ["missing-reset", "daily", "unlimited", "remaining", "usage", "exhausted", "insufficient"],
)
async def test_key_status_requires_finite_nonreset_funded_evidence(change):
    data = {"limit": 1, "limit_remaining": 1, "usage": 0, "limit_reset": None}
    if change == "missing-reset":
        del data["limit_reset"]
    elif change == "daily":
        data["limit_reset"] = "daily"
    elif change == "unlimited":
        data["limit"] = None
    elif change in ("remaining", "usage"):
        data["limit_remaining" if change == "remaining" else "usage"] = 2
    elif change == "exhausted":
        data["limit_remaining"] = 0
    else:
        data["limit_remaining"] = 0.1
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"data": data}))
    with pytest.raises(LocalFailure):
        await key_status("synthetic-only-key", transport, required="0.2")


async def test_get_only_billing_and_offline_full_history_recovery(tmp_path, isolated_key):
    campaign, expected = wire_campaign(tmp_path)
    sent = []
    cost = "0.000123456789012345678901234567890123456789"

    def gateway(request):
        sent.append(request)
        if request.method == "POST":
            return httpx.Response(200, content=response_bytes(cost))
        assert request.method == "GET" and request.url.path == "/api/v1/generation"
        assert request.url.params["id"] == "gen-0"
        return httpx.Response(
            200,
            content=(
                '{"data":{"id":"gen-0","model":"synthetic/model",'
                '"provider_name":"Synthetic Gateway Display","upstream_id":"upstream-id",'
                '"total_cost":' + cost + "}}"
            ).encode(),
        )

    with campaign:
        campaign.reserve("case-0")
        campaign.dispatch("case-0")
        handler = ObservedHTTPHandler(
            expected,
            "synthetic-only-key",
            lambda obs: campaign.observe(
                "case-0",
                {**obs, "model": obs["response_model"], "upstream": campaign.expected_manifest["attempts"][0]["upstream"]},
            ),
            transport=httpx.MockTransport(gateway),
        )
        try:
            await handler.client.post(
                ENDPOINT + "/chat/completions",
                json=expected,
                headers={"Authorization": "Bearer synthetic-only-key"},
            )
        finally:
            await handler.close()
        await reconcile(campaign, "case-0", transport=httpx.MockTransport(gateway))
        assert campaign.state.settled == Decimal(cost)
        assert campaign.state.reserved == 0
        assert not campaign.state.blockers
    with Journal(campaign.directory, campaign.expected_manifest) as reopened:
        assert set(reopened.state.answers) == {"case-0"}
        assert reopened.state.answers["case-0"]["content"] == "answer"
        assert reopened.state.settled == Decimal(cost)
    assert [req.method for req in sent] == ["POST", "GET"]


@pytest.mark.parametrize(
    "change", ["id", "model", "provider_name", "total_cost", "status", "malformed", "error", "null-error"]
)
async def test_invalid_get_receipts_retain_funded_liability(tmp_path, isolated_key, change):
    campaign, _ = wire_campaign(tmp_path)
    data = {
        "id": "gen-0",
        "model": "synthetic/model",
        "provider_name": "Synthetic Gateway Display",
        "total_cost": 0.0001,
    }
    if change in ("id", "model", "provider_name"):
        data[change] = "different"
    elif change == "total_cost":
        data[change] = -1
    envelope = {"data": data}
    if change in ("error", "null-error"):
        envelope["error"] = None if change == "null-error" else {"code": 500, "message": "lookup failed"}
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(
            503 if change == "status" else 200,
            content=b"garbled" if change == "malformed" else json.dumps(envelope).encode(),
        )
    )
    with campaign:
        campaign.reserve("case-0")
        campaign.dispatch("case-0")
        campaign.observe(
            "case-0",
            {
                "generation_id": "gen-0",
                "model": "synthetic/model",
                "upstream": "Synthetic",
                "content": "answer",
                "usage": {"cost": "0.0001"},
            },
        )
        with pytest.raises(LocalFailure):
            await reconcile(campaign, "case-0", transport=transport)
        assert len(campaign.attempt("case-0")["receipts"]) == 1
        assert campaign.state.reserved == Decimal("0.1") and campaign.state.blockers
    with Journal(campaign.directory, campaign.expected_manifest) as reopened:
        assert reopened.state.reserved == Decimal("0.1") and reopened.state.blockers


async def test_receipt_only_recovery_needs_explicit_association_and_never_resends(
    tmp_path, isolated_key
):
    campaign, _ = wire_campaign(tmp_path)
    requests = []

    def gateway(request):
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "gen-0",
                    "model": "synthetic/model",
                    "provider_name": "Synthetic Gateway Display",
                    "total_cost": 0.0001,
                }
            },
        )

    transport = httpx.MockTransport(gateway)
    with campaign:
        campaign.reserve("case-0")
        campaign.dispatch("case-0")
        with pytest.raises(LocalFailure, match="association"):
            await reconcile(campaign, "case-0", transport=transport)
        assert not requests
        spec = campaign.attempt("case-0")["spec"]
        await reconcile(
            campaign,
            "case-0",
            transport=transport,
            association={
                "attempt": "case-0",
                "request_hash": spec["request_hash"],
                "generation_id": "gen-0",
                "evidence": "synthetic verified recovery record",
            },
        )
        assert campaign.state.settled == Decimal("0.0001")
        assert campaign.state.blockers == {"answer:case-0"}
        campaign.acknowledge("answer:case-0", "synthetic answer loss reviewed; never resend")
        assert not campaign.state.answers and not campaign.state.blockers
    assert len(requests) == 1


async def test_production_delegate_to_journal_billing_reopen_and_offline_replay(
    tmp_path, isolated_key, monkeypatch
):
    campaign, alias, params, contract, messages = delegate_campaign(tmp_path)
    model = campaign.expected_manifest["attempts"][0]["model"]
    requests = []

    def gateway(request):
        requests.append(request)
        if request.url.path == "/api/v1/key":
            return httpx.Response(
                200,
                json={"data": {"limit": 1, "limit_remaining": 1, "usage": 0, "limit_reset": None}},
            )
        if request.url.path == "/api/v1/generation":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "gen-0",
                        "model": model,
                        "provider_name": "Synthetic Gateway Display",
                        "total_cost": 0.0001,
                    }
                },
            )
        assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "id": "gen-0",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "provider": "Synthetic Gateway Display",
                "openrouter_metadata": routing_metadata(model, "Synthetic Gateway Display"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"score": 1}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                    "cost": 0.0001,
                },
            },
        )

    transport = httpx.MockTransport(gateway)
    with campaign:
        router = pinned.PinnedRouter(
            campaign, alias=alias, params=params, dispatch=True, transport=transport
        )
        router.bind(contract, 1, case_id="case-0")
        result = await router.route_call("judge", messages, temperature=0.0)
        assert result.success and result.content == '{"score": 1}'
        assert campaign.state.settled == Decimal("0.0001") and not campaign.state.blockers
    monkeypatch.delenv("GENESIS_QUALIFICATION_OPENROUTER_KEY")
    with Journal(campaign.directory, campaign.expected_manifest) as reopened:
        router = pinned.PinnedRouter(
            reopened, alias=alias, params=params, dispatch=False, transport=transport
        )
        router.bind(contract, 1, case_id="case-0")
        result = await router.route_call("judge", messages, temperature=0.0)
        assert result.success and result.content == '{"score": 1}'
    assert [r.method for r in requests] == ["GET", "POST", "GET"]
    assert "OPENROUTER_API_KEY" not in __import__("os").environ


@pytest.mark.parametrize("result_kind", ["false", "none", "truthy-integer"])
async def test_paid_observation_survives_unsuccessful_delegate_result(
    tmp_path, isolated_key, monkeypatch, result_kind
):
    from types import SimpleNamespace

    data, server = bundle(tmp_path), Gateway()
    original = pinned.LiteLLMDelegate.call

    async def fail_result(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        return (
            None
            if result_kind == "none"
            else SimpleNamespace(
                success=1 if result_kind == "truthy-integer" else False,
                error="synthetic false result",
            )
        )

    monkeypatch.setattr(pinned.LiteLLMDelegate, "call", fail_result)
    with data.campaign:
        with pytest.raises(LocalFailure):
            await ask(router(data, server), data.messages)
        assert data.campaign.attempt("case-1")["observations"][0]["content"]
        assert data.campaign.attempt("case-1")["failed"]
        with pytest.raises(LocalFailure, match="stopped"):
            await ask(router(data, server), data.messages, "case-2")
        await reconcile(data.campaign, "case-1", transport=server.transport)
        data.campaign.acknowledge("failure:case-1", "synthetic false result reviewed; no resend")
        assert (await ask(router(data, server, dispatch=False), data.messages)).content
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        "model",
        "endpoint",
        "generation_namespace",
        "params",
        "fingerprint",
        "coordinate",
        "request_hash",
        "unscheduled",
        "unfunded",
    ],
)
async def test_offline_frozen_binding_refusals_precede_credentials(tmp_path, monkeypatch, change):
    data, server = bundle(tmp_path), Gateway()
    manifest = data.campaign.expected_manifest
    binding = manifest["binding"]["transports"][data.alias]
    if change in ("model", "endpoint", "generation_namespace"):
        binding[change] = "changed"
    elif change == "params":
        binding["params"] = {**data.params, "upstream": "changed"}
    elif change == "fingerprint":
        manifest["attempts"][0]["credential_fingerprint"] = "0" * 64
    elif change == "coordinate":
        manifest["attempts"][1]["case_id"] = "case-1"
    elif change == "request_hash":
        manifest["attempts"][0]["request_hash"] = "0" * 64
    elif change == "unfunded":
        manifest["budget"] = "0.1"

    def no_credential():
        pytest.fail("credential accessed before offline refusal")

    monkeypatch.setattr(pinned, "dedicated_key", no_credential)
    with pytest.raises(Incomplete), data.campaign:
        await ask(
            router(data, server),
            data.messages,
            "case-4" if change == "unscheduled" else "case-1",
        )
    assert not server.requests and server.key_reads == 0


async def test_acknowledged_unsent_reservation_continues_same_attempt_without_reserving_twice(
    tmp_path, isolated_key
):
    data, server = bundle(tmp_path), Gateway()
    with data.campaign:
        data.campaign.reserve("case-1")
    with data.campaign:
        with pytest.raises(LocalFailure, match="stopped"):
            await ask(router(data, server), data.messages)
        data.campaign.acknowledge("reservation:case-1", "synthetic verified no dispatch")
        assert (await ask(router(data, server), data.messages)).content
        assert sum(line["kind"] == "reservation" for line in data.campaign.lines) == 1
    assert len(server.requests) == 1


async def test_request_checks_do_not_copy_full_campaign_per_event(
    tmp_path, isolated_key, monkeypatch
):
    data, server = bundle(tmp_path), Gateway()
    original = Journal.state.fget
    snapshots = []

    def state(self):
        snapshots.append(True)
        return original(self)

    with data.campaign:
        monkeypatch.setattr(Journal, "state", property(state))
        r = router(data, server)
        assert len(snapshots) == 1
        for case in ("case-1", "case-2", "case-3"):
            assert (await ask(r, data.messages, case)).content
        assert len(snapshots) == 1


@pytest.mark.parametrize("encoded", [False, True])
def test_raw_numeric_lexemes_survive_credential_redaction(monkeypatch, encoded):
    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    secret = (
        "".join("\\u" + format(ord(char), "04x") for char in "synthetic-only-key")
        if encoded
        else "synthetic-only-key"
    )
    raw = ('{"cost":1.2300e-4,"content":"echo ' + secret + '"}').encode()
    result = pinned.safe_body(raw)
    assert "1.2300e-4" in result
    assert json.loads(result)["content"] == "echo [REDACTED]"


@pytest.mark.parametrize("surface", ["message", "exception", "stack"])
@pytest.mark.parametrize("encoded", [False, True])
def test_diagnostic_factory_redacts_json_credentials(monkeypatch, surface, encoded):
    secret = "synthetic-diagnostic-key"
    monkeypatch.setenv(pinned.KEY_ENV, secret)
    spelling = "".join("\\u" + format(ord(char), "04x") for char in secret) if encoded else secret
    text = 'RAW RESPONSE: {"content":"echo ' + spelling + '","cost":0.0001000}'
    original = logging.getLogRecordFactory()
    error = ValueError(text)
    with pinned.private_logs():
        record = logging.getLogRecordFactory()(
            "synthetic", logging.DEBUG, __file__, 1,
            text if surface == "message" else "ordinary message", (),
            (ValueError, error, None) if surface == "exception" else None,
            sinfo=text if surface == "stack" else None,
        )
        result = logging.Formatter().format(record)
        assert secret not in result and spelling not in result
        assert "[REDACTED]" in result and "0.0001000" in result
    assert logging.getLogRecordFactory() is original


@pytest.mark.parametrize("failed", [False, True])
async def test_actual_delegate_debug_logs_redact_escaped_response(
    tmp_path, isolated_key, caplog, capsys, monkeypatch, failed
):
    data, server = bundle(tmp_path), Gateway(answer="echo synthetic-only-key")
    secret = "synthetic-only-key"
    escaped = "".join("\\u" + format(ord(char), "04x") for char in secret)
    monkeypatch.setattr(pinned.litellm, "set_verbose", True)
    caplog.set_level(logging.DEBUG, logger="LiteLLM")
    caplog.set_level(logging.DEBUG, logger="genesis.routing.litellm_delegate")

    def transport(request):
        response = server(request)
        if request.method != "POST":
            return response
        raw = response.content if not failed else json.dumps({"error": {"message": secret}}).encode()
        return httpx.Response(500 if failed else 200, content=raw.replace(secret.encode(), escaped.encode()))

    with data.campaign:
        if failed:
            with pytest.raises(LocalFailure):
                await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
        else:
            result = await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
            assert result.content == "echo [REDACTED]"
        await asyncio.sleep(0.05)
        assert secret not in caplog.text and escaped not in caplog.text
        assert escaped not in (data.campaign.directory / "answers.jsonl").read_text()
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err
        assert escaped not in captured.out + captured.err
        assert pinned.litellm.set_verbose is True
    assert len(server.requests) == 1


async def test_wire_numeric_boolean_is_not_equal_to_frozen_integer():
    expected = {"model": "synthetic/model", "messages": [], "max_tokens": 1}
    sent = []
    handler = ObservedHTTPHandler(
        expected,
        "synthetic-key",
        lambda _value: None,
        transport=httpx.MockTransport(lambda req: sent.append(req)),
    )
    try:
        with pytest.raises(LocalFailure, match="differs"):
            await handler.client.post(
                ENDPOINT + "/chat/completions",
                json={**expected, "max_tokens": True},
                headers={"Authorization": "Bearer synthetic-key"},
            )
        assert not sent
    finally:
        await handler.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_billing_get_failure_preserves_answer_and_pauses_campaign(
    tmp_path, isolated_key, cancelled
):
    data, server = bundle(tmp_path), Gateway()

    def transport(request):
        if request.url.path.endswith("/generation"):
            if cancelled:
                raise asyncio.CancelledError()
            raise httpx.ReadTimeout("synthetic billing read failure")
        return server(request)

    with data.campaign:
        with pytest.raises(asyncio.CancelledError if cancelled else LocalFailure):
            await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
        assert data.campaign.attempt("case-1")["observations"]
        assert data.campaign.attempt("case-1")["failed"]
        with pytest.raises(LocalFailure, match="stopped"):
            await ask(router(data, server), data.messages, "case-2")
        await reconcile(data.campaign, "case-1", transport=server.transport)
        data.campaign.acknowledge("failure:case-1", "synthetic billing recovery inspected")
        assert (await ask(router(data, server, dispatch=False), data.messages)).content
    assert len(server.requests) == 1


async def test_observation_sync_uncertainty_poisons_reads_and_prevents_billing(
    tmp_path, isolated_key, monkeypatch
):
    import os

    data, server = bundle(tmp_path), Gateway()
    original_observe = Journal.observe
    original_fsync = os.fsync

    def uncertain_observe(self, attempt, value):
        def fail_sync(fd):
            original_fsync(fd)
            raise OSError("synthetic uncertain observation sync")

        with monkeypatch.context() as context:
            context.setattr(os, "fsync", fail_sync)
            return original_observe(self, attempt, value)

    monkeypatch.setattr(Journal, "observe", uncertain_observe)
    with data.campaign:
        with pytest.raises(LocalFailure):
            await ask(router(data, server), data.messages)
        with pytest.raises(Incomplete, match="uncertain write"):
            data.campaign.ready()
        assert server.receipt_reads == 0
    with Journal(data.campaign.directory, data.campaign.expected_manifest) as reopened:
        assert reopened.attempt("case-1")["observations"]
        assert reopened.state.reserved == Decimal("0.1") and reopened.state.blockers
    assert len(server.requests) == 1


async def test_cancellation_meaning_survives_failure_evidence_append_error(
    tmp_path, isolated_key, monkeypatch
):
    data, server = bundle(tmp_path), Gateway()

    def transport(request):
        if request.method == "POST":
            raise asyncio.CancelledError()
        return server(request)

    def fail(_self, *_args, **_kwargs):
        raise OSError("synthetic failure append error")

    monkeypatch.setattr(Journal, "fail", fail)
    with data.campaign:
        with pytest.raises(asyncio.CancelledError) as error:
            await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
        assert any("append failed" in note for note in error.value.__notes__)
        assert data.campaign.state.blockers and data.campaign.state.reserved == Decimal("0.1")


@pytest.mark.parametrize("generation", [None, "", [], {}])
async def test_missing_or_malformed_observed_generation_refuses_get_before_credentials(
    tmp_path, monkeypatch, generation
):
    campaign, _ = wire_campaign(tmp_path)

    def no_key():
        pytest.fail("key accessed for invalid generation")

    monkeypatch.setattr(pinned, "dedicated_key", no_key)
    with campaign:
        campaign.reserve("case-0")
        campaign.dispatch("case-0")
        campaign.observe("case-0", {"generation_id": generation, "content": "answer"})
        with pytest.raises(LocalFailure, match="generation"):
            await reconcile(campaign, "case-0")


async def test_reconciliation_key_rotation_refuses_before_get(tmp_path, isolated_key, monkeypatch):
    campaign, _ = wire_campaign(tmp_path)
    monkeypatch.setenv(pinned.KEY_ENV, "different-synthetic-key")
    with campaign:
        campaign.reserve("case-0")
        campaign.dispatch("case-0")
        campaign.observe("case-0", {"generation_id": "gen-0", "content": "answer"})
        with pytest.raises(LocalFailure, match="credential differs"):
            await reconcile(campaign, "case-0")
