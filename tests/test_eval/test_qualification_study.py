"""Read-only study planning; synthetic subset instruments confer no paid admission."""

import copy
import hashlib
import importlib
import json
import os

import pytest

from genesis.eval.qualification import run
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.qualification_lineage_fixtures import (
    primitive_data,  # noqa: F401
    reject_one,
    retain_one,
)


def api():
    assert importlib.util.find_spec("genesis.eval.qualification.study_report") is not None, (
        "study planning is not implemented"
    )
    return importlib.import_module("genesis.eval.qualification.study_report")


def plan_for(data, histories, *, reuse=None):
    """Build three logical repetitions over one synthetic transport coordinate."""
    module = api()
    admission = data.manifest["binding"]["admission"]
    checked = {k: copy.deepcopy(v) for k, v in admission.items() if k != "runner_sources"}
    checked.update(models={data.alias: data.manifest["attempts"][0]["model"]},
                   parameters={data.alias: data.params}, complete=True)
    manifest = copy.deepcopy(data.manifest)
    manifest["binding"]["admission"] = run.admission_identity(checked)
    original = manifest["attempts"][0]
    manifest["attempts"] = [{**original, "id": f"logical-{rep}", "repetition": rep}
                            for rep in run.REPETITIONS]
    manifest["budget"] = "3"
    plan = {
        "version": 3, "protocol": "bounded_continuation_v1",
        "study_root": str(data.temp / "study"), **module.source_identity(),
        "financial_promise": "operating_budget_with_overrun_risk",
        "credential_mode": "shared_existing",
        "credential_fingerprint": original["credential_fingerprint"],
        "combined_allowance": "5", "price_snapshot": {
            "observed_at": "2026-01-01T00:00:00Z", "evidence": "Synthetic pricing only"},
        "models": {data.alias: {"manifest": manifest}}, "histories": histories,
        "expense_evidence": [], "reuse": reuse or {data.alias: {}},
        "max_total_replacements": 1, "max_new_attempts": 3, "cooldown_seconds": 0,
    }
    return plan, checked


@pytest.mark.asyncio
async def test_first_answer_is_reused_even_when_malformed(data):
    module = api()
    ref, spec, _gateway = await retain_one(data, answer="malformed judgment")
    # Runner changes alone are permitted; the underlying admission still matches.
    declared = {data.alias: {"logical-1": {"journal_sha256": ref["journal_sha256"],
                                          "attempt": spec["id"]}}}
    plan, checked = plan_for(data, [ref], reuse=declared)
    with module.Study(plan, checked, data.cases) as study:
        assert study.reuse == declared
        assert study.missing == {data.alias: ["logical-2", "logical-3"]}
        assert len(study.rows[data.alias]) == 1
        assert study.rows[data.alias]["logical-1"]["observations"][0]["content"] == "malformed judgment"
        assert study.financial()["unresolved_attempts"] == 0
        assert study.preview()["status"] == "pass"
    assert not (data.temp / "study").exists()


@pytest.mark.asyncio
async def test_eligible_answer_cannot_be_omitted_from_reuse_map(data):
    module = api()
    ref, _spec, _gateway = await retain_one(data)
    plan, checked = plan_for(data, [ref])
    with pytest.raises(Incomplete):
        module.Study(plan, checked, data.cases)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("max_new_attempts", True), ("max_new_attempts", 2),
    ("max_total_replacements", -1), ("cooldown_seconds", 601),
    ("version", 2), ("credential_mode", "dedicated"),
    ("source_commit", "f" * 40), ("runner_sources", {}),
    ("combined_allowance", "0.01"), ("unexpected", True),
])
async def test_closed_plan_controls_refuse(data, field, value):
    module = api()
    ref, spec, _gateway = await retain_one(data)
    declared = {data.alias: {"logical-1": {"journal_sha256": ref["journal_sha256"],
                                          "attempt": spec["id"]}}}
    plan, checked = plan_for(data, [ref], reuse=declared)
    plan[field] = value
    with pytest.raises(Incomplete):
        module.Study(plan, checked, data.cases)


@pytest.mark.asyncio
async def test_history_duplicate_and_changed_semantics_refuse(data):
    module = api()
    ref, _spec, _gateway = await retain_one(data)
    plan, checked = plan_for(data, [ref, ref])
    with pytest.raises(Incomplete):
        module.Study(plan, checked, data.cases)
    plan, checked = plan_for(data, [ref])
    manifest = plan["models"][data.alias]["manifest"]
    manifest["binding"]["admission"]["corpus_hash"] = digest("changed")
    with pytest.raises(Incomplete):
        module.Study(plan, checked, data.cases)


@pytest.mark.asyncio
async def test_exact_funding_boundary_and_existing_root_are_not_approvals(data):
    module = api()
    ref, spec, _gateway = await retain_one(data)
    declared = {data.alias: {"logical-1": {"journal_sha256": ref["journal_sha256"], "attempt": spec["id"]}}}
    plan, checked = plan_for(data, [ref], reuse=declared)
    plan["combined_allowance"] = "0.3001"
    with module.Study(plan, checked, data.cases) as study:
        assert study.preview()["status"] == "pass"
        os.mkdir(plan["study_root"], 0o700)
        assert study.preview()["status"] == "incomplete"
    plan["combined_allowance"] = "0.30009999999999999999999999999999999"
    with pytest.raises(Incomplete):
        module.Study(plan, checked, data.cases)


def test_known_rejection_consumes_one_replacement_without_settling_cost(data):
    module = api()
    ref, _spec = reject_one(data)
    # Freeze current shared parameters independently of the retained journal.
    data.manifest["binding"]["transports"][data.alias]["credential_mode"] = "shared_existing"
    data.manifest["attempts"][0]["credential_mode"] = "shared_existing"
    # That historical credential mode differs: it must remain nonreplaceable.
    plan, checked = plan_for(data, [ref])
    plan["max_new_attempts"] = 4
    with module.Study(plan, checked, data.cases) as study:
        assert study.preview()["status"] == "incomplete"
        assert study.financial()["unresolved_attempts"] == 1
        assert study.financial()["settled_plus_reserved"] == "0.1"


@pytest.mark.asyncio
async def test_auxiliary_receipt_is_hash_and_owner_bound_never_a_settlement(data):
    module = api()
    ref, spec, _gateway = await retain_one(data)
    declared = {data.alias: {"logical-1": {"journal_sha256": ref["journal_sha256"], "attempt": spec["id"]}}}
    plan, checked = plan_for(data, [ref], reuse=declared)
    path = data.temp / "expense.json"
    raw = json.dumps({"data": {"id": "gen-1", "model": spec["model"],
                               "provider_name": spec["receipt_provider"], "total_cost": 0.0001}})
    path.write_text(raw)
    path.chmod(0o600)
    entry = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
             "journal_sha256": ref["journal_sha256"], "attempt": spec["id"],
             "request_hash": spec["request_hash"], "generation_id": "gen-1",
             "model": spec["model"], "provider": spec["receipt_provider"]}
    plan["expense_evidence"] = [entry]
    with module.Study(plan, checked, data.cases) as study:
        assert study.financial()["raw_reported_cost"] == "0.0001"
        path.write_text("{}")
        with pytest.raises(Incomplete):
            study.check(final=True)
    path.write_text(raw)
    for key, value in (("sha256", "f" * 64), ("request_hash", "f" * 64), ("generation_id", "wrong")):
        changed = copy.deepcopy(plan)
        changed["expense_evidence"][0][key] = value
        with pytest.raises(Incomplete):
            module.Study(changed, checked, data.cases)


@pytest.mark.asyncio
async def test_actual_rendering_scores_retained_prefix_without_fabricated_dispatches(data):
    from genesis.eval.qualification import corpus, pinned, preflight

    module = api()
    rendered = await preflight.prepare(data.cases, data.temp)
    spec = data.manifest["attempts"][0]
    prepared = rendered[spec["contract"]][spec["case_id"]]
    configured = data.params[corpus.route(spec["contract"])]
    configured.update({k: v for k, v in prepared["call_parameters"].items() if k in pinned.ROUTE_KEYS})
    data.manifest["binding"]["admission"]["plan"] = rendered
    data.checked["plan"] = rendered
    spec.update(prompt_hash=prepared["prompt_hash"], request_hash=digest(
        pinned.request_body(spec["model"], data.params, spec["contract"], prepared["messages"])))
    ref, spec, gateway = await retain_one(data, answer='{"score": 0}')
    declared = {data.alias: {"logical-1": {"journal_sha256": ref["journal_sha256"], "attempt": spec["id"]}}}
    plan, checked = plan_for(data, [ref], reuse=declared)
    with module.Study(plan, checked, data.cases) as study:
        result = await module.report(study, data.cases, data.temp)
    repetitions = result["models"][data.alias]["contracts"][spec["contract"]]["repetitions"]
    assert repetitions[0]["correct"] == 1 and repetitions[0]["unscored"] == 0
    assert all(r["unscored"] == 1 for r in repetitions[1:])
    assert result["physical_history"]["dispatch"] == 1
    assert result["status"] == "incomplete"
    assert result["lineage"]["reused"] == 1 and result["lineage"]["missing"] == 2
    assert len(gateway.requests) == 1
    assert "history" not in result["models"][data.alias]


@pytest.mark.asyncio
async def test_selection_is_first_eligible_not_best_grade(data):
    module = api()
    first, spec, _ = await retain_one(data, answer="malformed grade")
    second_data = copy.copy(data)
    second_data.path = data.temp / "second"
    second, _, _ = await retain_one(second_data, answer='{"score": 1}', generation_prefix="second-")
    declared = {data.alias: {"logical-1": {"journal_sha256": first["journal_sha256"], "attempt": spec["id"]}}}
    plan, checked = plan_for(data, [first, second], reuse=declared)
    with module.Study(plan, checked, data.cases) as study:
        assert study.rows[data.alias]["logical-1"]["observations"][0]["content"] == "malformed grade"
    plan["reuse"][data.alias]["logical-1"]["journal_sha256"] = second["journal_sha256"]
    with pytest.raises(Incomplete, match="reuse_map_changed"):
        module.Study(plan, checked, data.cases)


@pytest.mark.asyncio
async def test_cross_journal_generation_collision_refuses(data):
    module = api()
    first, _spec, _ = await retain_one(data)
    second_data = copy.copy(data)
    second_data.path = data.temp / "collision"
    second, _, _ = await retain_one(second_data)
    plan, checked = plan_for(data, [first, second])
    with pytest.raises(Incomplete, match="generation_collision"):
        module.Study(plan, checked, data.cases)


def test_precise_rejection_uses_existing_replacement_slot_and_retains_liability(data):
    module = api()
    data.manifest["binding"]["transports"][data.alias]["credential_mode"] = "shared_existing"
    data.manifest["attempts"][0]["credential_mode"] = "shared_existing"
    ref, _ = reject_one(data)
    plan, checked = plan_for(data, [ref])
    with module.Study(plan, checked, data.cases) as study:
        assert study.preview()["status"] == "pass"
        assert len(study.replaced) == 1
        assert study.financial()["unresolved_attempts"] == 1
        assert study.liability == module.accounting.currency("0.1")
        assert study.pending == module.accounting.currency("0.3")
    plan["max_total_replacements"] = 0
    with pytest.raises(Incomplete, match="attempt_cap_invalid"):
        module.Study(plan, checked, data.cases)


@pytest.mark.asyncio
async def test_public_inspect_rejects_invalid_references_without_credentials(data, monkeypatch):
    from genesis.eval.qualification import pinned

    module = api()
    monkeypatch.setattr(pinned, "credential", lambda *_args, **_kwargs: pytest.fail("credential read forbidden"))
    plan_path = data.temp / "plan.json"
    plan_path.write_text("{}")
    plan_path.chmod(0o600)
    options = dict.fromkeys(("reference_policy", "current_reference_policy", "reference_approval", "source_files"))
    with pytest.raises(Incomplete):
        await module.inspect(plan_path, data.cases, data.temp, candidates={data.alias: data.params}, options=options)
    assert os.environ[pinned.KEY_ENV] == "synthetic-only-key"
    assert plan_path.read_text() == "{}"
    assert not (data.temp / "study").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("contradiction", ["model", "error"])
async def test_equal_price_does_not_hide_contradictory_receipt(data, contradiction):
    module = api()
    ref, spec, _ = await retain_one(data)
    declared = {data.alias: {"logical-1": {"journal_sha256": ref["journal_sha256"], "attempt": spec["id"]}}}
    plan, checked = plan_for(data, [ref], reuse=declared)
    body = {"data": {"id": "gen-1", "model": spec["model"],
                     "provider_name": spec["receipt_provider"], "total_cost": 0.0001}}
    if contradiction == "model":
        body["data"]["model"] = "contradictory-synthetic-model"
    else:
        body["error"] = {"code": 500}
    path = data.temp / "contradictory-price.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    plan["expense_evidence"] = [{
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "journal_sha256": ref["journal_sha256"], "attempt": spec["id"],
        "request_hash": spec["request_hash"], "generation_id": "gen-1",
        "model": body["data"]["model"], "provider": spec["receipt_provider"],
    }]
    with pytest.raises(Incomplete, match="expense"):
        module.Study(plan, checked, data.cases)
