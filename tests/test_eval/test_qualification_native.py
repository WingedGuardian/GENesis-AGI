"""Native transport guards with synthetic inputs and MockTransport only."""

import asyncio
import json
import logging
import os
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import litellm
import pytest

from genesis.eval.qualification import (
    corpus,
    native,
    native_expense,
    pinned,
    preflight,
    transports,
)
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_native_fixtures import fixture, native_environment  # noqa: F401


def test_fixed_profiles_keep_missing_marker_legacy_only():
    assert transports.select(native.ALIAS) is native
    assert transports.select("openrouter-mimo", {}) is pinned
    assert transports.select(native.ALIAS, {"transport_kind": native.KIND}) is native
    for alias, binding in ((native.ALIAS, {}), (native.ALIAS, {"transport_kind": None}),
                           (native.ALIAS, {"transport_kind": "unknown"}),
                           ("openrouter-mimo", {"transport_kind": native.KIND}),
                           (native.ALIAS, [])):
        with pytest.raises(Incomplete):
            transports.select(alias, binding)


def test_retained_profile_selection_does_not_reread_routing(monkeypatch):
    monkeypatch.setattr(pinned, "routing", lambda: pytest.fail("retained row reread routing configuration"))
    assert transports.select(native.ALIAS, {"transport_kind": native.KIND}) is native
    assert transports.select("openrouter-mimo", {}) is pinned


@pytest.mark.parametrize("field,value", [("provider_type", "openrouter"),
                                         ("model_id", "deepseek-chat"),
                                         ("base_url", native.ENDPOINT + "/beta"),
                                         ("params", {"seed": 1})])
def test_native_alias_rejects_implicit_or_different_route(field, value):
    cfg = replace(native.resolve(native.ALIAS), **{field: value})
    with pytest.raises(Incomplete):
        native.resolve(native.ALIAS, SimpleNamespace(providers={native.ALIAS: cfg}))


@pytest.mark.parametrize("field,value", [("temperature", True), ("temperature", 1),
                                         ("top_p", False), ("top_p", 0.9),
                                         ("max_tokens", True), ("max_tokens", 999),
                                         ("thinking", {"type": "enabled"}),
                                         ("thinking", {"type": "disabled", "budget": 1}),
                                         ("seed", 0), ("provider", {})])
def test_native_wire_controls_are_closed(field, value):
    params, *_ = fixture()
    params["judge"][field] = value
    with pytest.raises(Incomplete):
        native.validate_params(params)


def test_rendered_request_and_delegate_kwargs_match_measured_wire():
    params, contract, messages, body, *_ = fixture()
    assert set(body) == {"model", "messages", "temperature", "top_p", "max_tokens", "thinking"}
    assert body["messages"] == messages and body["max_tokens"] == 1000
    assert type(body["temperature"]) is float and type(body["top_p"]) is float
    assert native.request_body(native.MODEL, params, "j9_relevance", messages)["max_tokens"] == 150
    kwargs = native.delegate_kwargs(params, contract, body)
    assert kwargs.pop("api_base") == native.ENDPOINT
    extra = kwargs.pop("extra_body")
    assert {"model": native.MODEL, "messages": messages, **kwargs, **extra} == body


@pytest.mark.parametrize("temperature", [0, 0.0, -0.0])
@pytest.mark.parametrize("top_p", [1, 1.0])
def test_native_preflight_freezes_effective_numbers_without_mutating_input(temperature, top_p):
    params, contract, *_ = fixture()
    for role in pinned.ROUTES:
        params[role].update(temperature=temperature, top_p=top_p)
    before = deepcopy(params)
    prepared = {contract: {"synthetic": {"call_parameters": {"temperature": 0.0}}},
                corpus.RELEVANCE: {"synthetic-j9": {"call_parameters": {
                    "temperature": 0.0, "max_tokens": 150, "chain_offset": 0}}}}
    effective = preflight.parameters(native.ALIAS, params, prepared)
    assert all(type(effective[role][key]) is float for role in pinned.ROUTES
               for key in ("temperature", "top_p"))
    assert pinned.canonical(params) == pinned.canonical(before)
    assert all(effective[role]["temperature"] == 0.0 and effective[role]["top_p"] == 1.0
               for role in pinned.ROUTES)


def test_frozen_binding_and_attempt_require_native_identity_and_tariff():
    params, _, _, _, binding, spec, _ = fixture()
    native.validate_binding(binding, native.resolve(native.ALIAS), params)
    native.validate_attempt(spec, binding)
    for field, value in (("transport_kind", "unknown"), ("credential_mode", "dedicated"),
                         ("generation_namespace", "openrouter"), ("receipt_provider", "invented"),
                         ("provider_identity_evidence", ""), ("credential_fingerprint", "not-a-hash")):
        changed = {**binding, field: value}
        with pytest.raises(Incomplete):
            native.validate_binding(changed, native.resolve(native.ALIAS), params)
    for field, value in (("transport_kind", "unknown"), ("native_tariff", {}),
                         ("endpoint", native.ENDPOINT + "/beta/chat/completions"),
                         ("credential_fingerprint", "c" * 64), ("receipt_model", native.MODEL)):
        with pytest.raises(Incomplete):
            native.validate_attempt({**spec, field: value}, binding)


@pytest.mark.parametrize("name", ["callbacks", "input_callback", "success_callback", "failure_callback",
                                  "_async_input_callback", "_async_success_callback", "_async_failure_callback",
                                  "pre_call_rules", "post_call_rules", "payload_print"])
def test_callback_refusal_precedes_native_key_read(monkeypatch, name):
    if name == "payload_print":
        monkeypatch.setenv("LITELLM_PRINT_STANDARD_LOGGING_PAYLOAD", "0")
    else:
        monkeypatch.setattr(litellm, name, [object()])
    original = os.environ.get

    def no_key_read(key, *args):
        assert key != native.KEY_ENV, "key read before callback guard"
        return original(key, *args)

    monkeypatch.setattr(os.environ, "get", no_key_read)
    with pytest.raises(pinned.LocalFailure, match="fresh CLI process"):
        native.credential()


@pytest.mark.parametrize("secret", ["x", "123", "synthetic-native-only-key"])
def test_native_key_scope_redacts_threads_and_restores_aliases(monkeypatch, caplog, secret):
    import threading

    monkeypatch.setenv(native.KEY_ENV, secret)
    before = {key: os.environ.get(key) for key in native.PRODUCTION_KEY_NAMES}
    factory = logging.getLogRecordFactory()
    with caplog.at_level(logging.WARNING), pinned.private_logs(secrets=(secret,)):
        assert native.credential() == secret
        with native.scoped_credential(secret):
            assert os.environ["DEEPSEEK_API_KEY"] == secret
            assert not any(key in os.environ for key in native.PRODUCTION_KEY_NAMES if key != "DEEPSEEK_API_KEY")
        thread = threading.Thread(target=logging.getLogger("native.synthetic").warning,
                                  args=("synthetic key=%s", secret))
        thread.start()
        thread.join()
        assert pinned.failure_message(RuntimeError(secret)) == "[REDACTED]"
    assert all(secret not in record.getMessage() for record in caplog.records)
    assert {key: os.environ.get(key) for key in native.PRODUCTION_KEY_NAMES} == before
    assert logging.getLogRecordFactory() is factory


async def observe(raw=None, *, status=200, headers=None, callback=None):
    _, _, _, body, _, spec, default = fixture()
    rows = []
    handler = native.HTTPHandler(body, "synthetic-key", callback or rows.append,
                                 transport=httpx.MockTransport(lambda request: httpx.Response(
                                     status, json=default if raw is None else raw, headers=headers)))
    try:
        await handler.client.post(native.COMPLETION_URL, json=body,
                                  headers={"Authorization": "Bearer synthetic-key"})
    finally:
        await handler.close()
    return rows[0], spec, handler


async def test_actual_native_body_observed_before_callback_returns():
    observation, spec, handler = await observe()
    assert handler.posts == 1 and observation["generation_id"] == "synthetic-native-generation"
    assert observation["model"] == native.MODEL and observation["upstream"] == "deepseek"
    assert "usage" not in observation and "cost" not in json.loads(observation["raw_body"])["usage"]
    native.verify_observation(observation, spec)
    assert native_expense.observation_interval(spec, observation)[1] > 0


@pytest.mark.parametrize("field,value", [("id", ""), ("model", "wrong-model"),
                                         ("system_fingerprint", 42), ("usage", {})])
async def test_raw_identity_and_usage_failures_are_visible(field, value):
    *_, raw = fixture()
    raw[field] = value
    observation, spec, _ = await observe(raw)
    assert json.loads(observation["raw_body"])[field] == value
    with pytest.raises(Incomplete):
        native.verify_observation(observation, spec)


async def test_fingerprints_are_diagnostics_not_automatically_pinned():
    for fingerprint in (None, "synthetic-backend-a", "synthetic-backend-b"):
        *_, raw = fixture()
        raw["system_fingerprint"] = fingerprint
        observation, spec, _ = await observe(raw)
        native.verify_observation(observation, spec)
        assert observation["system_fingerprint"] == fingerprint


async def test_header_generation_contradiction_rejects_answer():
    observation, spec, _ = await observe(headers={"x-generation-id": "contradictory"})
    with pytest.raises(Incomplete):
        native.verify_observation(observation, spec)


@pytest.mark.parametrize("failure", ["refusal", "extra-choice", "missing-content"])
async def test_unusable_answer_can_retain_calculable_native_usage(failure):
    *_, raw = fixture()
    if failure == "refusal":
        raw["choices"][0]["finish_reason"] = "content_filter"
    elif failure == "extra-choice":
        raw["choices"].append(deepcopy(raw["choices"][0]))
    else:
        raw["choices"][0]["message"]["content"] = None
    observation, spec, _ = await observe(raw)
    assert native_expense.billing_evidence(spec, observation)["calculated_upper"]
    with pytest.raises(Incomplete):
        native.verify_observation(observation, spec)


@pytest.mark.parametrize("refusal", ["synthetic-blocked-image", False, 0, [], {}])
async def test_refusal_field_rejects_usable_looking_retained_content(refusal):
    *_, raw = fixture()
    raw["choices"][0]["message"]["refusal"] = refusal
    observation, spec, _ = await observe(raw)
    assert native_expense.billing_evidence(spec, observation)["calculated_upper"]
    with pytest.raises(Incomplete):
        native.verify_observation(observation, spec)


async def test_finalization_appends_calculation_without_any_network():
    observation, spec, _ = await observe()
    writes = []
    receipts = []
    journal = SimpleNamespace(attempt=lambda _: {"spec": spec, "observations": [observation],
                                                "lookups": [], "receipts": receipts},
                              settle=lambda attempt, evidence: (writes.append((attempt, evidence)),
                                                               receipts.append(evidence)))
    evidence = await native.finalize(journal, spec["id"], transport=object())
    assert writes == [(spec["id"], evidence)]
    assert evidence["cost_basis"] == native_expense.BASIS and "charge" not in evidence
    assert await native.finalize(journal, spec["id"]) == evidence
    assert len(writes) == 1
    receipts.append({**evidence, "calculated_upper": "0"})
    with pytest.raises(Incomplete, match="contradicts"):
        await native.finalize(journal, spec["id"])


@pytest.mark.parametrize("fault", ["url", "method", "auth", "body", "retry"])
async def test_common_egress_guard_rejects_native_wire_changes(fault):
    _, _, _, body, *_ = fixture()
    posts = []
    handler = native.HTTPHandler(body, "synthetic-key", lambda _: None,
                                 transport=httpx.MockTransport(lambda request: (
                                     posts.append(request) or httpx.Response(200, json=fixture()[-1]))))
    url, method, key, actual = native.COMPLETION_URL, "POST", "synthetic-key", body
    if fault == "url":
        url = native.ENDPOINT + "/beta/chat/completions"
    elif fault == "method":
        method = "GET"
    elif fault == "auth":
        key = "different-key"
    elif fault == "body":
        actual = {**body, "seed": 1}
    try:
        if fault == "retry":
            await handler.client.post(url, json=actual, headers={"Authorization": "Bearer " + key})
        with pytest.raises(Incomplete):
            await handler.client.request(method, url, json=actual, headers={"Authorization": "Bearer " + key})
    finally:
        await handler.close()
    assert len(posts) == (1 if fault == "retry" else 0)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_observer_failure_and_cancellation_propagate_after_observation(cancelled):
    error = asyncio.CancelledError() if cancelled else RuntimeError("synthetic callback failure")
    recorded = []

    def fail(observation):
        recorded.append(observation)
        raise error

    with pytest.raises(type(error)):
        await observe(callback=fail)
    assert recorded[0]["generation_id"] == "synthetic-native-generation"


@pytest.mark.parametrize("mode", ["success", "429", "timeout", "invalid-json"])
@pytest.mark.parametrize("integer_controls", [False, True])
async def test_actual_delegate_makes_one_native_attempt(monkeypatch, mode, integer_controls):
    params, contract, messages, body, _, spec, raw = fixture()
    if integer_controls:
        params["judge"].update(temperature=0, top_p=1)
    requests, observations = [], []
    callbacks = list(litellm.callbacks)

    def serve(request):
        requests.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic native timeout")
        if mode == "429":
            return httpx.Response(429, json={"error": {"message": "synthetic rejection"}})
        if mode == "invalid-json":
            return httpx.Response(200, content=b"{")
        return httpx.Response(200, json=raw)

    handler = native.HTTPHandler(body, "synthetic-native-key", observations.append,
                                 transport=httpx.MockTransport(serve))
    with pinned.private_logs(secrets=("synthetic-native-key",)), native.scoped_credential("synthetic-native-key"):
        try:
            result = await pinned.LiteLLMDelegate(pinned.routing()).call(
                native.ALIAS, native.MODEL, messages, client=handler,
                num_retries=0, max_retries=0, success_callback=[], failure_callback=[],
                caching=False, timeout=pinned.COMPLETION_TIMEOUT_S,
                **native.delegate_kwargs(params, contract, body))
        finally:
            await handler.close()
    assert len(requests) == handler.posts == 1
    assert str(requests[0].url) == native.COMPLETION_URL
    assert json.loads(requests[0].content) == body
    assert not any(key.lower().startswith("x-openrouter") for key in requests[0].headers)
    assert result.success is (mode == "success")
    assert litellm.callbacks == callbacks
    if mode == "success":
        native.verify_observation(observations[0], spec)
