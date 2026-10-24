"""Explicit shared-key policy; every key, consent and gateway here is synthetic."""
import copy
import json
import os
from contextlib import ExitStack

import httpx
import pytest

from genesis.eval.qualification import execution, pinned
from genesis.eval.qualification.accounting import Journal, currency
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.qualification_transport_fixtures import Gateway, ask, bundle, router
from tests.test_eval.test_qualification_operating import population_plan, write_input
from tests.test_eval.test_qualification_pinned import env as transport_env

MODE = "shared_existing"
KEY = "synthetic-only-key"


@pytest.fixture
def env(monkeypatch, tmp_path):
    return transport_env.__wrapped__(monkeypatch, tmp_path)


def shared_manifest(manifest):
    for transport in manifest["binding"]["transports"].values():
        transport["credential_mode"] = MODE
    for spec in manifest["attempts"]:
        spec["credential_mode"] = MODE


@pytest.fixture
def plan(tmp_path):
    # Full schedule geometry, not a claim of complete source/reference admission.
    result = population_plan(tmp_path, execution.PRO_COMPARISON)
    result.value.update(version=2, credential_mode=MODE)
    result.value.pop("expected_limit")
    result.value.pop("baseline_usage")
    for model in result.value["models"].values():
        shared_manifest(model["manifest"])
    result.consent["plan_hash"] = digest(result.value)
    return result


def test_shared_plan_admits_without_inventing_exclusive_key_observations(plan):
    before = copy.deepcopy(plan.value)
    frozen = execution.validate(plan.value, plan.consent, plan.checked)
    assert sum(len(m["attempts"]) for m in frozen.values()) == 4500
    assert plan.value == before and "expected_limit" not in before and "baseline_usage" not in before
    assert all(m["binding"]["operating_plan_hash"] == digest(before) for m in frozen.values())


@pytest.mark.parametrize("change", ["version", "mode", "mode_absent", "extra_limit", "extra_usage",
                                  "transport", "spec", "missing_spec", "fingerprint", "consent"])
def test_shared_version_mode_and_frozen_population_must_agree(plan, change):
    execution.validate(plan.value, plan.consent, plan.checked)
    manifest = plan.value["models"][execution.PRO_COMPARISON[0]]["manifest"]
    if change == "version":
        plan.value["version"] = 3
    elif change == "mode":
        plan.value["credential_mode"] = "dedicated"
    elif change == "mode_absent":
        plan.value.pop("credential_mode")
    elif change in ("extra_limit", "extra_usage"):
        plan.value["expected_limit" if change == "extra_limit" else "baseline_usage"] = "5"
    elif change == "transport":
        manifest["binding"]["transports"][execution.PRO_COMPARISON[0]]["credential_mode"] = "dedicated"
    elif change == "spec":
        manifest["attempts"][0]["credential_mode"] = "unknown"
    elif change == "missing_spec":
        manifest["attempts"][0].pop("credential_mode")
    elif change == "fingerprint":
        plan.value["credential_fingerprint"] = "0" * 64
    else:
        plan.consent["scope"] = "2250_requests"
    plan.consent["plan_hash"] = digest(plan.value)
    with pytest.raises(Incomplete):
        execution.validate(plan.value, plan.consent, plan.checked)


@pytest.mark.parametrize("production", ["environment", "stored"])
def test_shared_credential_is_explicit_and_default_dedicated_still_refuses(env, monkeypatch, production):
    if production == "environment":
        monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    else:
        (env / "secrets.env").write_text("OPENROUTER_API_KEY=" + KEY + "\n")
    assert pinned.credential(MODE) == KEY
    with pytest.raises(Incomplete, match="production"):
        pinned.credential()
    with pytest.raises(Incomplete):
        pinned.credential("unknown")
    monkeypatch.delenv(pinned.KEY_ENV)
    with pytest.raises(Incomplete):
        pinned.credential(MODE)


@pytest.mark.parametrize("dispatch", [False, True])
def test_frozen_shared_marker_cannot_silently_authorize_default_dispatch(env, dispatch):
    data, gateway = bundle(env), Gateway()
    shared_manifest(data.campaign.expected_manifest)
    assert pinned.credential(MODE) == KEY
    with data.campaign:
        if dispatch:
            with pytest.raises(Incomplete):
                router(data, gateway, dispatch=True)
            with pytest.raises(Incomplete, match="controller"):
                router(data, gateway, dispatch=True, credential_mode=MODE)
        else:
            assert router(data, gateway, dispatch=False).credential_mode == MODE
            assert router(data, gateway, dispatch=False, credential_mode=MODE).credential_mode == MODE
        with pytest.raises(Incomplete):
            router(data, gateway, dispatch=dispatch, credential_mode="unknown")
    assert not gateway.requests and gateway.key_reads == 0


@pytest.mark.parametrize("observed,pending,accepted", [
    ("0", "4.5", True), ("1", "4", True), ("1.0000000000000000001", "4", False),
])
def test_shared_operating_predicate_uses_only_experiment_receipts(plan, observed, pending, accepted):
    execution.validate(plan.value, plan.consent, plan.checked)
    control = execution.Operating.__new__(execution.Operating)
    control.plan = plan.value
    control.observed, control.pending = currency(observed), currency(pending)
    if accepted:
        control.predicate({"credential_mode": MODE})
    else:
        with pytest.raises(Incomplete):
            control.predicate({"credential_mode": MODE})


@pytest.mark.parametrize("mode", ["settled", "payment_required", "timeout", "over_allocation"])
async def test_validated_shared_controller_preserves_actual_writes_and_no_resend(plan, env, monkeypatch, mode):
    execution.validate(plan.value, plan.consent, plan.checked)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    class SharedGateway(Gateway):
        def __call__(self, request):
            assert not request.url.path.endswith("/key"), "shared traffic has no exclusive usage snapshot"
            if request.method == "POST" and mode == "timeout":
                self.requests.append(request)
                raise httpx.ReadTimeout("synthetic timeout " + KEY)
            response = super().__call__(request)
            if mode == "over_allocation" and request.method == "POST":
                body = response.json()
                body["usage"]["cost"] = 0.003
                self.generations[body["id"]]["total_cost"] = 0.003
                return httpx.Response(200, json=body)
            return response

    gateway = SharedGateway(status=402 if mode == "payment_required" else 200)
    with ExitStack() as stack:
        left, right = (write_input(plan.tmp / filename, value) for filename, value in (
            ("plan.json", plan.value), ("consent.json", plan.consent)))
        stack.callback(left.close)
        stack.callback(right.close)
        control = execution.Operating(left, right, plan.checked)
        journals = {a: stack.enter_context(Journal(plan.tmp / a, control.manifests[a])) for a in control.aliases}
        control.attach(journals)
        adapters = [control.router(journals[a], alias=a, params=plan.checked["parameters"][a],
                                   dispatch=True, transport=gateway.transport) for a in control.aliases]
        contract, rows = next(iter(plan.checked["plan"].items()))
        for adapter in adapters:
            adapter.bind(contract, 1, case_id="case-0")

        def no_campaign_snapshot(_journal):
            raise AssertionError("dispatch/recovery must not copy the complete campaign per request")

        monkeypatch.setattr(Journal, "state", property(no_campaign_snapshot))
        if mode == "settled":
            assert (await ask(adapters[0], rows["case-0"]["messages"], "case-0")).success
            assert control.observed == currency("0.0001")
            monkeypatch.delenv(pinned.KEY_ENV)
            assert (await ask(adapters[0], rows["case-0"]["messages"], "case-0")).success
            monkeypatch.setenv(pinned.KEY_ENV, KEY)
            await pinned.reconcile(journals[control.aliases[0]], "a-0-1", transport=gateway.transport)
            assert gateway.receipt_reads == 2
        else:
            with pytest.raises(Incomplete):
                await ask(adapters[0], rows["case-0"]["messages"], "case-0")
            with pytest.raises(Incomplete):
                await ask(adapters[1], rows["case-0"]["messages"], "case-0")
            assert control.stopped
            assert all(KEY not in json.dumps(j.lines) for j in journals.values())
        assert len(gateway.requests) == 1 and gateway.key_reads == 0
        assert os.environ["OPENROUTER_API_KEY"] == KEY
        dispatches = [line for j in journals.values() for line in j.lines if line["kind"] == "dispatch"]
        assert len(dispatches) == 1 and dispatches[0]["key_status"] == {"credential_mode": MODE}


async def test_shared_receipt_only_recovery_remains_missing_answer_and_cannot_resend(env, monkeypatch):
    data, gateway = bundle(env), Gateway()
    shared_manifest(data.campaign.expected_manifest)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assert pinned.credential(MODE) == KEY
    with data.campaign:
        missing = "case-2"
        data.campaign.reserve(missing)
        data.campaign.dispatch(missing)
        spec = data.campaign.attempt(missing)["spec"]
        gateway.generations["lost-answer"] = {"id": "lost-answer", "model": spec["model"],
            "provider_name": spec["receipt_provider"], "total_cost": 0.0001}
        association = {
            "attempt": missing, "request_hash": spec["request_hash"], "generation_id": "lost-answer",
            "evidence": "SYNTHETIC verified association only"}
        with pytest.raises(Incomplete, match="mode"):
            await pinned.reconcile(data.campaign, missing, transport=gateway.transport,
                                   credential_mode="dedicated", association=association)
        monkeypatch.setenv(pinned.KEY_ENV, "different-synthetic-key")
        with pytest.raises(Incomplete, match="credential differs"):
            await pinned.reconcile(data.campaign, missing, transport=gateway.transport, association=association)
        monkeypatch.setenv(pinned.KEY_ENV, KEY)
        assert gateway.receipt_reads == 0
        await pinned.reconcile(data.campaign, missing, transport=gateway.transport, association=association)
        assert not data.campaign.attempt(missing)["observations"]
        monkeypatch.delenv(pinned.KEY_ENV)
        adapter = router(data, gateway, dispatch=False)
        with pytest.raises(Incomplete):
            await ask(adapter, data.messages, missing)
    assert not gateway.requests and gateway.key_reads == 0 and gateway.receipt_reads == 1


def test_shared_mode_keeps_callback_isolation_before_credentials(env, monkeypatch):
    import litellm

    assert pinned.credential(MODE) == KEY
    monkeypatch.setattr(litellm, "callbacks", ["synthetic observer"])
    with pytest.raises(Incomplete):
        pinned.credential(MODE)


def test_shared_plan_cannot_be_relabelled_as_legacy(plan):
    execution.validate(plan.value, plan.consent, plan.checked)
    plan.value.pop("credential_mode")
    plan.value.update(version=1, expected_limit="5", baseline_usage="0")
    plan.consent["plan_hash"] = digest(plan.value)
    with pytest.raises(Incomplete):
        execution.validate(plan.value, plan.consent, plan.checked)
