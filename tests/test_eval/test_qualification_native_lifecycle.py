"""Real serial adapter and immutable native evidence, without provider access."""

import asyncio
import hashlib
import json
import logging
import threading
import traceback
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from genesis.eval.qualification import (
    continuation,
    corpus,
    execution,
    lineage,
    native,
    pinned,
    preflight,
    run,
    study_report,
)
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_native_fixtures import (  # noqa: F401
    campaign,
    fixture,
    native_environment,
    study_plan,
)


def adapter(data, transport):
    async def admitted(_adapter, _attempt, info):
        assert info == {"credential_mode": "shared_existing"}

    result = pinned.PinnedRouter(data.journal, alias=native.ALIAS, params=data.params, dispatch=True,
                                 transport=transport, credential_mode="shared_existing",
                                 dispatch_admission=admitted)
    result.bind(data.contract, 1, case_id=data.spec["case_id"])
    return result


async def ask(data, transport):
    return await adapter(data, transport).route_call("synthetic-native-qualification", data.messages)


async def test_numeric_key_keeps_real_journal_identity_usage_and_exact_cost(tmp_path, monkeypatch):
    data = campaign(tmp_path)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    assert data.secret in data.spec["request_hash"]
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json=data.raw)

    with data.journal:
        result = await ask(data, httpx.MockTransport(serve))
        row = data.journal.attempt(data.spec["id"])
        assert result.content == '{"score":1}'
        assert row["observations"][0]["request_hash"] == data.spec["request_hash"]
        assert row["observations"][0]["native_usage"] == data.raw["usage"]
        assert row["totals"] == (Decimal("0.00000248"), Decimal(0))
        assert lineage.eligible_row(data.journal.state, data.spec["id"], data.manifest)
    assert len(requests) == 1 and requests[0].method == "POST"
    assert [e["kind"] for e in data.journal.lines] == [
        "manifest", "reservation", "dispatch", "observation", "billing"]
    raw = json.loads(data.journal.lines[3]["evidence"]["raw_body"])
    assert raw["usage"] == data.raw["usage"] and "cost" not in raw["usage"]


@pytest.mark.parametrize("stage", ["delegate", "close", "finalize"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_late_failure_keeps_calculation_and_redacts_short_secret(
    tmp_path, monkeypatch, caplog, stage, cancelled
):
    data = campaign(tmp_path)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    error = asyncio.CancelledError(data.secret) if cancelled else RuntimeError(data.secret)
    owner, name = ((pinned.LiteLLMDelegate, "call") if stage == "delegate"
                   else (native.HTTPHandler, "close") if stage == "close" else (native, "finalize"))
    original = getattr(owner, name)

    async def late(*args, **kwargs):
        await original(*args, **kwargs)
        thread = threading.Thread(target=logging.getLogger("native.synthetic").warning,
                                  args=("key=%s", data.secret))
        thread.start()
        thread.join()
        raise error

    monkeypatch.setattr(owner, name, late)
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json=data.raw)

    with caplog.at_level(logging.WARNING), data.journal:
        with pytest.raises(asyncio.CancelledError if cancelled else pinned.LocalFailure) as failure:
            await ask(data, httpx.MockTransport(serve))
        if cancelled:
            assert failure.value is error
        assert str(failure.value) == "[REDACTED]"
        row = data.journal.attempt(data.spec["id"])
        assert row["receipts"] and row["totals"][1] == 0
        assert row["failed"] and lineage.eligible_row(data.journal.state, data.spec["id"], data.manifest) is None
        assert data.secret not in " ".join(e.get("error", "") for e in data.journal.lines)
    assert all(data.secret not in record.getMessage() for record in caplog.records)
    assert len(requests) == 1


async def test_primary_cancellation_survives_secondary_calculation_failure(tmp_path, monkeypatch):
    data = campaign(tmp_path)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    original = pinned.LiteLLMDelegate.call
    primary = asyncio.CancelledError("synthetic primary")

    async def cancel_after(*args, **kwargs):
        await original(*args, **kwargs)
        raise primary

    async def failed_calculation(*args, **kwargs):
        raise RuntimeError(data.secret)

    monkeypatch.setattr(pinned.LiteLLMDelegate, "call", cancel_after)
    monkeypatch.setattr(native, "finalize", failed_calculation)
    with data.journal:
        with pytest.raises(asyncio.CancelledError) as failure:
            await ask(data, httpx.MockTransport(lambda _: httpx.Response(200, json=data.raw)))
        assert failure.value is primary
        row = data.journal.attempt(data.spec["id"])
        assert row["totals"][1] == Decimal(data.spec["max_charge"])
        assert [e["error"] for e in data.journal.lines if e["kind"] == "failure"] == [
            "synthetic primary", "[REDACTED]"]


async def test_wrong_native_header_keeps_full_reservation(tmp_path, monkeypatch):
    data = campaign(tmp_path)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    with data.journal:
        with pytest.raises(Incomplete):
            await ask(data, httpx.MockTransport(lambda _: httpx.Response(
                200, json=data.raw, headers={"x-generation-id": "foreign-generation"})))
        row = data.journal.attempt(data.spec["id"])
        assert row["observations"][0]["header_generation_id"] == "foreign-generation"
        assert not row["receipts"] and row["totals"][1] == Decimal(data.spec["max_charge"])


@pytest.mark.parametrize("module_name", ["pinned", "native", "transports", "native_expense"])
async def test_provider_modules_are_rechecked_production_identity(tmp_path, monkeypatch, module_name):
    from tests.test_eval.test_qualification_preflight import admitted

    modules = preflight.production_modules()
    module = next(m for m in modules if m.__name__.endswith("." + module_name))
    cases, options = admitted(tmp_path, monkeypatch)
    original, changed = preflight.file_hash, False

    def source_hash(path):
        return "0" * 64 if changed and str(path) == module.__file__ else original(path)

    async def prepare(*args, **kwargs):
        nonlocal changed
        changed = True
        return {}

    monkeypatch.setattr(preflight, "file_hash", source_hash)
    monkeypatch.setattr(preflight, "prepare", prepare)
    with pytest.raises(Incomplete, match="source or contract changed"):
        await preflight.check(cases, tmp_path, **options)


def test_legacy_operating_population_still_refuses_native():
    with pytest.raises(Incomplete):
        execution.selected_aliases({native.ALIAS: {}})
    assert execution.selected_aliases({native.ALIAS: {}}, allow_native=True) == (native.ALIAS,)


def test_native_reuse_identity_includes_marker_and_tariff():
    *_, spec, _ = fixture()
    assert study_report.same_request(spec, deepcopy(spec))
    for field, value in (("transport_kind", "other"), ("native_tariff", {}),
                         ("cost_basis", "other"), ("endpoint", pinned.ENDPOINT),
                         ("credential_fingerprint", hashlib.sha256(b"other").hexdigest())):
        assert not study_report.same_request(spec, {**spec, field: value})


async def test_bounded_controller_owns_native_child_and_closed_readback(tmp_path, monkeypatch):
    data, plan, checked, cases, consent = study_plan(tmp_path)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json=data.raw)

    manifest = plan["models"][native.ALIAS]["manifest"]
    with pytest.raises(Incomplete, match="native_requires_bounded_study"):
        run.validate_campaign(manifest, checked, native.ALIAS, cases=cases)
    with study_report.Study(plan, checked, cases) as study:
        controller = continuation.Controller(study, consent, inputs=[])
        with controller:
            spec = manifest["attempts"][0]
            messages = checked["plan"][spec["contract"]][spec["case_id"]]["messages"]
            body = native.request_body(native.MODEL, data.params, spec["contract"], messages)
            answer = await controller.acquire(native.ALIAS, spec["id"], body,
                                              transport=httpx.MockTransport(serve))
            assert answer["content"] == '{"score":1}'
        terminal = controller.finish(admission_current=True)
        assert terminal["status"] == "incomplete" and terminal["children"][0]["safe"]
    assert len(requests) == 1 and requests[0].method == "POST"
    with study_report.Study(plan, checked, cases) as study:
        assert study_report.include_current(study)["status"] == "incomplete"
        assert len(study.rows[native.ALIAS]) == 1
        assert study.financial()["unresolved_attempts"] == 0
    with (pytest.raises(Incomplete, match="exists"),
          study_report.Study(plan, checked, cases) as study,
          continuation.Controller(study, consent, inputs=[])):
        pytest.fail("closed study was relaunched")


@pytest.mark.parametrize("field,value", [("max_total_replacements", 1), ("max_new_attempts", 2251),
                                         ("histories", [{}]), ("expense_evidence", [{}])])
def test_native_study_refuses_replacement_or_historical_acquisition(tmp_path, field, value):
    _, plan, checked, cases, _ = study_plan(tmp_path)
    plan[field] = value
    with pytest.raises(Incomplete, match="fresh_population_without_replacements"):
        study_report.Study(plan, checked, cases)


@pytest.mark.parametrize("secret", ["s7x", "773"])
async def test_cancelled_explicit_cause_is_redacted_outside_secret_scope(tmp_path, monkeypatch, secret):
    data = campaign(tmp_path, secret=secret)
    monkeypatch.setenv(native.KEY_ENV, secret)
    primary = asyncio.CancelledError(secret)
    primary.add_note("synthetic private note: " + secret)
    original = pinned.LiteLLMDelegate.call

    async def cancel_with_cause(*args, **kwargs):
        await original(*args, **kwargs)
        raise primary from RuntimeError(secret)

    monkeypatch.setattr(pinned.LiteLLMDelegate, "call", cancel_with_cause)
    with data.journal:
        with pytest.raises(asyncio.CancelledError) as failure:
            await ask(data, httpx.MockTransport(lambda _: httpx.Response(200, json=data.raw)))
        assert failure.value is primary
    monkeypatch.delenv(native.KEY_ENV)
    assert secret not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize("fault", [
    "reasoning-content", "reasoning-count", "nested-reasoning-count", "safety-zero", "safety-count",
    "nested-safety-zero", "nested-safety-count", "title", "empty-title", "tool-calls",
    "server-tool-calls", "annotations", "judge-cap", "relevance-cap",
])
@pytest.mark.parametrize("secret", ["7", "_"])
async def test_reported_controls_contradicting_native_wire_hold_reservation(tmp_path, monkeypatch, fault, secret):
    data = campaign(tmp_path, secret=secret, contract=corpus.RELEVANCE if fault == "relevance-cap" else None)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    message, usage = data.raw["choices"][0]["message"], data.raw["usage"]
    if fault == "reasoning-content":
        message["reasoning_content"] = "Synthetic unexpected reasoning"
    elif fault in ("reasoning-count", "safety-zero", "safety-count"):
        usage["reasoning_tokens" if fault == "reasoning-count" else "safety_tokens"] = int(fault != "safety-zero")
    elif fault.startswith("nested-"):
        usage["completion_tokens_details"] = {
            "reasoning_tokens" if fault == "nested-reasoning-count" else "safety_tokens": int(fault != "nested-safety-zero")}
    elif fault in ("title", "empty-title"):
        message["title"] = "Synthetic title" if fault == "title" else ""
    elif fault in ("tool-calls", "server-tool-calls", "annotations"):
        message[fault.replace("-", "_")] = [{"synthetic": "unexpected feature"}]
    else:
        usage["completion_tokens"] = 151 if fault == "relevance-cap" else 1001
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json=data.raw)

    with data.journal:
        with pytest.raises(Incomplete):
            await ask(data, httpx.MockTransport(serve))
        row = data.journal.attempt(data.spec["id"])
        assert row["failed"] and not row["receipts"]
        assert row["totals"][1] == Decimal(data.spec["max_charge"])
        assert json.loads(row["observations"][0]["raw_body"]) == data.raw
        assert lineage.eligible_row(data.journal.state, data.spec["id"], data.manifest) is None
    assert len(requests) == 1


@pytest.mark.parametrize("contract", [None, corpus.RELEVANCE])
@pytest.mark.parametrize("reasoning", [None, ""])
async def test_native_empty_optional_controls_and_exact_output_cap_are_valid(tmp_path, monkeypatch, contract, reasoning):
    data = campaign(tmp_path, contract=contract)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    data.raw["choices"][0]["message"].update(reasoning_content=reasoning, tool_calls=[], server_tool_calls=[], annotations=[])
    usage = data.raw["usage"]
    usage.update(completion_tokens=150 if contract == corpus.RELEVANCE else 1000,
                 reasoning_tokens=0, completion_tokens_details={"reasoning_tokens": 0})
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    with data.journal:
        await ask(data, httpx.MockTransport(lambda _: httpx.Response(200, json=data.raw)))
        assert lineage.eligible_row(data.journal.state, data.spec["id"], data.manifest)
        assert data.journal.attempt(data.spec["id"])["totals"][1] == 0


@pytest.mark.parametrize("fault", ["refusal", "late-failure"])
async def test_report_retains_fingerprint_of_ineligible_physical_answer(tmp_path, monkeypatch, fault):
    data, plan, checked, cases, consent = study_plan(tmp_path)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    data.raw["system_fingerprint"] = "synthetic-ineligible-backend"
    if fault == "refusal":
        data.raw["choices"][0]["finish_reason"] = "content_filter"
    else:
        original = pinned.LiteLLMDelegate.call

        async def fail_after(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("synthetic late failure")

        monkeypatch.setattr(pinned.LiteLLMDelegate, "call", fail_after)
    with study_report.Study(plan, checked, cases) as study:
        controller = continuation.Controller(study, consent, inputs=[])
        with controller:
            spec = study.manifests[native.ALIAS]["attempts"][0]
            messages = checked["plan"][spec["contract"]][spec["case_id"]]["messages"]
            body = native.request_body(native.MODEL, data.params, spec["contract"], messages)
            with pytest.raises(Incomplete):
                await controller.acquire(native.ALIAS, spec["id"], body, transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json=data.raw)))
        assert controller.finish(admission_current=True)["status"] == "incomplete"
    with study_report.Study(plan, checked, cases) as study:
        # Isolate diagnostic reporting while retaining the full admitted schedule
        # and real closed physical journal. Full scoring is exercised by CLI E2E.
        selected = {data.contract: [cases[data.contract][0]]}
        result = await study_report.report(study, selected, tmp_path)
        assert not study.rows[native.ALIAS]
        assert result["status"] == "incomplete" and result["physical_history"]["dispatch"] == 1
        assert result["financial"]["unresolved_attempts"] == 0
        assert result["models"][native.ALIAS]["system_fingerprints"] == ["synthetic-ineligible-backend"]


@pytest.mark.parametrize("refusal", ["synthetic-blocked-image", None, ""])
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
@pytest.mark.parametrize("secret", ["7", "_"])
async def test_explicit_refusal_is_ineligible_but_usage_remains_calculable(
    tmp_path, monkeypatch, refusal, finish_reason, secret
):
    data = campaign(tmp_path, secret=secret)
    monkeypatch.setenv(native.KEY_ENV, data.secret)
    data.raw["choices"][0]["finish_reason"] = finish_reason
    data.raw["choices"][0]["message"]["refusal"] = refusal
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json=data.raw)

    with data.journal:
        if refusal:
            with pytest.raises(Incomplete):
                await ask(data, httpx.MockTransport(serve))
        else:
            await ask(data, httpx.MockTransport(serve))
        row = data.journal.attempt(data.spec["id"])
        assert row["receipts"] and row["totals"] == (Decimal("0.00000248"), Decimal(0))
        assert bool(row["failed"]) == bool(refusal)
        assert bool(lineage.eligible_row(data.journal.state, data.spec["id"], data.manifest)) == (not refusal)
        assert json.loads(row["observations"][0]["raw_body"])["choices"][0]["message"]["refusal"] == refusal
    assert len(requests) == 1
