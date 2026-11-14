"""Unresolved expense identity and overruns; all evidence and HTTP are synthetic."""

import copy
import hashlib
import json

import httpx
import pytest

from genesis.eval.qualification import accounting, pinned, study_report
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_lineage_fixtures import (
    primitive_data,  # noqa: F401
    reference,
    reject_one,
    retain_one,
)
from tests.test_eval.qualification_transport_fixtures import Gateway
from tests.test_eval.test_qualification_study import plan_for


def shared(data):
    data.manifest["binding"]["transports"][data.alias]["credential_mode"] = "shared_existing"
    data.manifest["attempts"][0]["credential_mode"] = "shared_existing"


def auxiliary(data, ref, spec, *, model, charge):
    body = {"data": {"id": "synthetic-rejection", "model": model,
                     "provider_name": spec["receipt_provider"], "total_cost": charge}}
    path = data.temp / "synthetic-auxiliary-expense.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "journal_sha256": ref["journal_sha256"], "attempt": spec["id"],
            "request_hash": spec["request_hash"], "generation_id": "synthetic-rejection",
            "model": model, "provider": spec["receipt_provider"]}


@pytest.mark.parametrize("provided", ["receipt", "wire", "unrelated"])
def test_unresolved_auxiliary_model_uses_frozen_receipt_namespace(data, provided):
    shared(data)
    spec = data.manifest["attempts"][0]
    binding = data.manifest["binding"]["transports"][data.alias]
    for target in (spec, binding):
        target.update(backend_model=spec["model"], receipt_model="synthetic-receipt-namespace")
    ref, spec = reject_one(data)
    plan, checked = plan_for(data, [ref])
    model = {"receipt": spec["receipt_model"], "wire": spec["model"],
             "unrelated": "unrelated-synthetic-model"}[provided]
    plan["expense_evidence"] = [auxiliary(data, ref, spec, model=model, charge=0.01)]
    if provided == "receipt":
        with study_report.Study(plan, checked, data.cases) as study:
            assert study.financial()["raw_reported_cost"] == "0.01"
            assert study.financial()["unresolved_attempts"] == 1
    else:
        with pytest.raises(Incomplete, match="expense_association"):
            study_report.Study(plan, checked, data.cases)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["auxiliary", "captured"])
@pytest.mark.parametrize("prior_answer", [False, True])
async def test_overallocation_remains_reportable_and_stops_execution(data, origin, prior_answer):
    shared(data)
    histories, reuse = [], {data.alias: {}}
    if prior_answer:
        first, spec, _ = await retain_one(data)
        histories.append(first)
        reuse[data.alias]["logical-1"] = {"journal_sha256": first["journal_sha256"], "attempt": spec["id"]}
        data = copy.copy(data)
        data.path = data.temp / "unresolved-sibling"
    ref, spec = reject_one(data)
    if origin == "captured":
        def receipt(request):
            assert request.method == "GET" and request.url.path == "/api/v1/generation"
            return httpx.Response(200, json={"data": {
                "id": "synthetic-rejection", "model": spec["model"],
                "provider_name": spec["receipt_provider"], "total_cost": 0.2}})

        with accounting.Journal(data.path, data.manifest) as journal:
            await pinned.reconcile(journal, spec["id"], transport=httpx.MockTransport(receipt),
                                   credential_mode="shared_existing")
        ref = reference(data)
    histories.append(ref)
    plan, checked = plan_for(data, histories, reuse=reuse)
    # The complete known rejection still consumes its one replacement slot;
    # the separate overrun stop must apply even with an earlier eligible answer.
    plan["max_new_attempts"] = 2 if prior_answer else 3
    if origin == "auxiliary":
        plan["expense_evidence"] = [auxiliary(data, ref, spec, model=spec["model"], charge=0.2)]
    with study_report.Study(plan, checked, data.cases) as study:
        financial = study.financial()
        assert financial["raw_reported_cost"] == ("0.2001" if prior_answer else "0.2")
        assert financial["unresolved_attempts"] == int(origin == "auxiliary")
        assert financial["over_allocation_attempts"] == int(origin == "captured")
        assert financial["settled_plus_reserved"] == ("0.1001" if prior_answer else "0.1")
        canonical = study.histories[ref["journal_sha256"]]._state
        assert canonical.reserved == accounting.currency("0.1") and canonical.settled == 0
        assert "historical_charge_exceeds_allocation" in study.execution_issues
        assert study.preview()["status"] == "incomplete"
        assert len(study.rows[data.alias]) == int(prior_answer)


@pytest.mark.asyncio
async def test_captured_wrong_model_cost_remains_raw_and_unsettled(data):
    shared(data)
    _, spec = reject_one(data)

    def receipt(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"data": {
            "id": "synthetic-rejection", "model": "wrong-captured-synthetic-model",
            "provider_name": spec["receipt_provider"], "total_cost": 0.01}})

    with accounting.Journal(data.path, data.manifest) as journal, pytest.raises(pinned.LocalFailure, match="identity"):
        await pinned.reconcile(journal, spec["id"], transport=httpx.MockTransport(receipt),
                               credential_mode="shared_existing")
    ref = reference(data)
    plan, checked = plan_for(data, [ref])
    plan["max_new_attempts"] = 4
    with study_report.Study(plan, checked, data.cases) as study:
        assert study.financial()["raw_reported_cost"] == "0.01"
        assert study.financial()["unresolved_attempts"] == 1
        assert study.liability == accounting.currency("0.1")
        assert study.preview()["status"] == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_answer", [False, True])
async def test_usage_overrun_without_receipt_stops_despite_prior_answer(data, prior_answer):
    shared(data)
    histories, reuse = [], {data.alias: {}}
    if prior_answer:
        first, spec, _ = await retain_one(data)
        histories.append(first)
        reuse[data.alias]["logical-1"] = {"journal_sha256": first["journal_sha256"], "attempt": spec["id"]}
        data = copy.copy(data)
        data.path = data.temp / "usage-overrun-sibling"
    gateway = Gateway()

    def response(request):
        if request.method == "GET":
            return httpx.Response(404, json={"error": "synthetic missing receipt"})
        body = gateway(request).json()
        body["id"] = "synthetic-usage-overrun"
        body["usage"]["cost"] = 0.2
        return httpx.Response(200, json=body)

    async def permit(_router, _attempt, _info):
        pass

    spec = data.manifest["attempts"][0]
    messages = data.checked["plan"][spec["contract"]][spec["case_id"]]["messages"]
    with accounting.Journal(data.path, data.manifest) as journal:
        router = pinned.PinnedRouter(journal, alias=data.alias, params=data.params,
                                    dispatch=True, credential_mode="shared_existing",
                                    dispatch_admission=permit, transport=httpx.MockTransport(response))
        router.bind(spec["contract"], spec["repetition"], case_id=spec["case_id"])
        with pytest.raises(pinned.LocalFailure):
            await router.route_call("synthetic-usage-overrun", messages)
    histories.append(reference(data))
    plan, checked = plan_for(data, histories, reuse=reuse)
    plan["max_new_attempts"] = 3 if prior_answer else 4
    with study_report.Study(plan, checked, data.cases) as study:
        assert "historical_charge_exceeds_allocation" in study.execution_issues
        assert study.preview()["status"] == "incomplete"
        assert study.financial()["unresolved_attempts"] == 1
        assert len(study.rows[data.alias]) == int(prior_answer)
    assert len(gateway.requests) == 1
