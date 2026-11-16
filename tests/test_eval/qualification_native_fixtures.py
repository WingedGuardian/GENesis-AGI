"""Public synthetic native identity, tariff and response fixtures."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from genesis.eval.qualification import corpus, native, native_expense
from genesis.eval.qualification.evidence import digest


@pytest.fixture(autouse=True)
def native_environment(monkeypatch, tmp_path):
    """Only synthetic approved and original resolver keys enter these probes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".genesis").mkdir(mode=0o700)
    for name in native.PRODUCTION_KEY_NAMES:
        monkeypatch.setenv(name, "synthetic-original-" + name.lower())
    monkeypatch.setenv(native.KEY_ENV, "synthetic-approved-native-key")
    empty = tmp_path / "empty-native-secrets.env"
    empty.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(empty))


def fixture():
    params = {"upstream": "deepseek", **{
        role: {"temperature": 0.0, "top_p": 1.0, "max_tokens": 150 if role == "relevance" else 1000,
               "thinking": {"type": "disabled"}} for role in ("judge", "relevance", "novelty")}}
    tariff = {"version": 1, "currency": "USD", "unit_tokens": 1000000, "provider": "deepseek",
              "model": native.MODEL, "selection": "published_tier_interval",
              "rates": {"off_peak": {"prompt_cache_hit": "0.01", "prompt_cache_miss": "0.1",
                                      "completion": "0.2"},
                        "peak": {"prompt_cache_hit": "0.02", "prompt_cache_miss": "0.2",
                                 "completion": "0.4"}},
              "pricing_evidence": {"url": "https://api-docs.deepseek.com/quick_start/pricing/",
                                   "sha256": "a" * 64, "observed_at": "2026-11-15T00:00:00Z"}}
    contract = next(k for k in corpus.versions() if k not in (corpus.RELEVANCE, corpus.NOVELTY))
    messages = [{"role": "user", "content": "Synthetic native question"}]
    body = native.request_body(native.MODEL, params, contract, messages)
    binding = {"transport_kind": native.KIND, "model": native.MODEL, "params": params,
               "endpoint": native.ENDPOINT, "generation_namespace": native.NAMESPACE,
               "credential_mode": "shared_existing", "credential_fingerprint": "b" * 64,
               "provider_identity_evidence": "synthetic dated first-party alias evidence",
               "cost_basis": native_expense.BASIS, "native_tariff": tariff}
    spec = {key: deepcopy(value) for key, value in binding.items() if key != "params"}
    spec.update(id="native-1", alias=native.ALIAS, upstream=native.NAMESPACE,
                endpoint=native.COMPLETION_URL, request_hash=digest(body), max_charge="0.01",
                contract=contract, version=corpus.versions()[contract], case_id="synthetic-1",
                repetition=1, prompt_hash=digest(messages))
    raw = {"id": "synthetic-native-generation", "object": "chat.completion", "created": 1,
           "model": native.MODEL, "system_fingerprint": "synthetic-backend-a",
           "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"score":1}'},
                        "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                     "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6}}
    return params, contract, messages, body, binding, spec, raw


def campaign(tmp_path, *, secret="7", contract=None):  # noqa: S107 — synthetic numeric redaction control
    import hashlib

    from genesis.eval.qualification import accounting

    params, default_contract, messages, body, binding, spec, raw = fixture()
    contract = contract or default_contract
    body = native.request_body(native.MODEL, params, contract, messages)
    spec.update(contract=contract, version=corpus.versions()[contract])
    # This numeric key must appear inside a trusted hash: otherwise the
    # association-redaction regression could pass without exercising its axis.
    if secret == "7":
        for _ in range(128):
            if secret in digest(body):
                break
            messages[0]["content"] += "."
            body = native.request_body(native.MODEL, params, contract, messages)
        assert secret in digest(body), "synthetic hash control did not exercise numeric secret"
    spec.update(request_hash=digest(body), prompt_hash=digest(messages),
                credential_fingerprint=hashlib.sha256(secret.encode()).hexdigest())
    binding["credential_fingerprint"] = spec["credential_fingerprint"]
    prepared = {contract: {spec["case_id"]: {"messages": messages, "prompt_hash": digest(messages)}}}
    manifest = {"version": 2, "budget": spec["max_charge"],
                "binding": {"transports": {native.ALIAS: binding}, "admission": {"plan": prepared}},
                "attempts": [spec]}
    return SimpleNamespace(journal=accounting.Journal(tmp_path / "native-child", manifest),
                           manifest=manifest, params=params, contract=contract, messages=messages,
                           body=body, binding=binding, spec=spec, raw=raw, secret=secret)


def study_plan(tmp_path):
    """Full schedule geometry; synthetic metadata is not real corpus admission."""
    from genesis.eval.qualification import run, study_report
    from tests.test_eval.qualification_fixtures import small_corpus

    data = campaign(tmp_path)
    original = small_corpus(novelty=False)[data.contract][0]
    cases, rendered, attempts = [], {}, []
    for index in range(750):
        identity = f"synthetic-{index}"
        case = {**deepcopy(original), "id": identity}
        messages = [{"role": "user", "content": f"Synthetic native question {index}"}]
        body = native.request_body(native.MODEL, data.params, data.contract, messages)
        rendered[identity] = {"messages": messages, "prompt_hash": digest(messages)}
        cases.append(case)
        attempts.extend({**deepcopy(data.spec), "id": f"{identity}-{rep}", "case_id": identity,
                         "repetition": rep, "prompt_hash": digest(messages), "request_hash": digest(body)}
                        for rep in run.REPETITIONS)
    checked = {"complete": True, "corpus_hash": digest(cases), "policy_hash": "a" * 64,
               "approval_hash": "b" * 64, "versions": {data.contract: data.spec["version"]},
               "rubrics": [], "reference_sources": {}, "production_sources": {},
               "plan": {data.contract: rendered}, "models": {native.ALIAS: native.MODEL},
               "parameters": {native.ALIAS: data.params}}
    manifest = {"version": 2, "budget": "22.5", "binding": {
        "transports": {native.ALIAS: data.binding}, "admission": run.admission_identity(checked)},
        "attempts": attempts}
    plan = {"version": 3, "protocol": study_report.PROTOCOL,
            "study_root": str(tmp_path / "native-study"), **study_report.source_identity(),
            "financial_promise": "operating_budget_with_overrun_risk", "credential_mode": "shared_existing",
            "credential_fingerprint": data.binding["credential_fingerprint"], "combined_allowance": "22.5",
            "price_snapshot": {"observed_at": "2026-11-15T00:00:00Z", "evidence": "Synthetic pricing"},
            "models": {native.ALIAS: {"manifest": manifest}}, "histories": [], "expense_evidence": [],
            "reuse": {native.ALIAS: {}}, "max_total_replacements": 0, "max_new_attempts": 2250,
            "cooldown_seconds": 0}
    consent = {"version": 2, "protocol": study_report.PROTOCOL, "plan_hash": digest(plan),
               "max_new_attempts": 2250, "author": "owner", "decision_evidence": "Synthetic authorization only"}
    return data, plan, checked, {data.contract: cases}, consent
