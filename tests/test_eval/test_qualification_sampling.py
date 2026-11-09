"""Sampled incumbents retain full reference admission, never full qualification."""
import copy
from types import SimpleNamespace

import pytest

from genesis.eval.qualification import corpus, execution, pinned, run
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.qualification_preflight_fixtures import params

PRO, FLASH = execution.PRO_COMPARISON


@pytest.fixture
def sample():
    """Synthetic schedule/class geometry only; not complete source admission."""
    cases = {}
    for name in corpus.versions():
        rows = []
        for i in range(6):
            row = {"id": f"case-{i}"}
            if name == corpus.NOVELTY:
                row["expected_target"] = None if i % 2 else "synthetic-target"
            else:
                row.update(user_passed=bool(i % 2), reference_provenance={"label_source": "human"})
            rows.append(row)
        cases[name] = rows
    configured = params()
    checked = {"corpus_hash": digest(cases), "policy_hash": "a" * 64,
               "approval_hash": "b" * 64, "versions": corpus.versions(), "rubrics": [],
               "reference_sources": {}, "production_sources": {},
               "models": {a: pinned.resolve(a).model_id for a in (PRO, FLASH)},
               "parameters": {a: configured for a in (PRO, FLASH)},
               "plan": {name: {r["id"]: {"messages": [{"role": "user", "content": name + r["id"]}],
                                          "prompt_hash": digest([{"role": "user", "content": name + r["id"]}])}
                                for r in rows} for name, rows in cases.items()}}
    marker = {"purpose": "incumbent_baseline", "corpus_hash": digest(cases),
              "case_ids": {name: [r["id"] for r in rows[:4]] for name, rows in cases.items()},
              "selection_evidence": "synthetic preselected manifest, not owner consent"}
    manifests = {}
    for alias in (PRO, FLASH):
        identity = {"model": checked["models"][alias], "params": configured,
                    "endpoint": pinned.ENDPOINT, "generation_namespace": "openrouter",
                    "credential_fingerprint": "d" * 64, "receipt_provider": "Synthetic Display",
                    "provider_identity_evidence": "synthetic only"}
        attempts = []
        for name, rows in checked["plan"].items():
            for case_id, row in rows.items():
                if alias == PRO and case_id not in marker["case_ids"][name]:
                    continue
                for rep in run.REPETITIONS:
                    attempts.append({"id": f"attempt-{len(attempts)}", "alias": alias,
                                     "model": identity["model"], "upstream": configured["upstream"],
                                     "endpoint": pinned.ENDPOINT + "/chat/completions",
                                     "request_hash": digest(pinned.request_body(identity["model"], configured, name, row["messages"])),
                                     "max_charge": "0.001", "generation_namespace": "openrouter",
                                     "contract": name, "version": corpus.versions()[name],
                                     "case_id": case_id, "repetition": rep, "prompt_hash": row["prompt_hash"],
                                     **{k: identity[k] for k in ("credential_fingerprint", "receipt_provider", "provider_identity_evidence")}})
        binding = {"admission": run.admission_identity(checked), "transports": {alias: identity}}
        if alias == PRO:
            binding["baseline_sample"] = marker
        manifests[alias] = {"version": 2, "budget": "1", "binding": binding, "attempts": attempts}
    plan = {"version": 1, "financial_promise": execution.PROMISE,
            "stop_semantics": execution.STOP, "combined_allowance": "2",
            "expected_limit": "2", "baseline_usage": "0", "credential_fingerprint": "d" * 64,
            "price_snapshot": {"observed_at": "synthetic", "evidence": "synthetic prices"},
            "models": {a: {"manifest": m} for a, m in manifests.items()}}
    consent = {"version": 1, "plan_hash": digest(plan), "scope": "240_requests", "author": "owner",
               "decision_evidence": "SYNTHETIC TEST ONLY, NOT SPENDING CONSENT"}
    return SimpleNamespace(cases=cases, checked=checked, marker=marker, manifests=manifests,
                           plan=plan, consent=consent)


def test_sampled_schedule_requires_actual_full_case_context(sample):
    run.validate_campaign(sample.manifests[PRO], sample.checked, PRO, cases=sample.cases)
    with pytest.raises(Incomplete):
        run.validate_campaign(sample.manifests[PRO], sample.checked, PRO)
    mutated = copy.deepcopy(sample.cases)
    mutated[next(iter(mutated))][0]["user_passed"] = True
    with pytest.raises(Incomplete):
        run.validate_campaign(sample.manifests[PRO], sample.checked, PRO, cases=mutated)


@pytest.mark.parametrize("change", ["empty", "unknown", "duplicate", "wrong_hash", "missing_contract",
                                  "one_class", "full_contract", "bad_type", "wrong_purpose", "extra_field"])
def test_every_invalid_selection_refuses(sample, change):
    marker = sample.marker
    name = next(iter(sample.cases))
    if change == "empty":
        marker["case_ids"][name] = []
    elif change == "unknown":
        marker["case_ids"][name][0] = "unknown"
    elif change == "duplicate":
        marker["case_ids"][name][0] = marker["case_ids"][name][1]
    elif change == "wrong_hash":
        marker["corpus_hash"] = "0" * 64
    elif change == "missing_contract":
        marker["case_ids"].pop(name)
    elif change == "one_class":
        marker["case_ids"][name] = ["case-0", "case-2"]
    elif change == "full_contract":
        marker["case_ids"][name] = [r["id"] for r in sample.cases[name]]
    elif change == "bad_type":
        marker["case_ids"][name][0] = True
    elif change == "wrong_purpose":
        marker["purpose"] = "qualification"
    else:
        marker["ignore_missing"] = True
    with pytest.raises(Incomplete):
        run.validate_campaign(sample.manifests[PRO], sample.checked, PRO, cases=sample.cases)


def test_operating_schedule_binds_actual_selected_population(tmp_path):
    from tests.test_eval.test_qualification_operating import population_plan

    # Reuse complete legacy schedule geometry: Flash remains all 2,250 attempts.
    plan = population_plan(tmp_path, execution.PRO_COMPARISON)
    name = next(iter(plan.checked["plan"]))
    cases = {name: [{"id": i, "user_passed": bool(index % 2),
                     "reference_provenance": {"label_source": "human"}}
                    for index, i in enumerate(plan.checked["plan"][name])]}
    plan.checked["corpus_hash"] = digest(cases)
    ids = [c["id"] for c in cases[name][:200]]
    for alias, model in plan.value["models"].items():
        manifest = model["manifest"]
        manifest["binding"]["admission"] = run.admission_identity(plan.checked)
        if alias == PRO:
            manifest["binding"]["baseline_sample"] = {
                "purpose": "incumbent_baseline", "corpus_hash": digest(cases),
                "case_ids": {name: ids}, "selection_evidence": "synthetic preselection only"}
            manifest["attempts"] = [r for r in manifest["attempts"] if r["case_id"] in ids]
    plan.consent.update(scope="2850_requests", plan_hash=digest(plan.value))
    manifests = execution.validate(plan.value, plan.consent, plan.checked, cases=cases)
    assert [len(manifests[a]["attempts"]) for a in (PRO, FLASH)] == [600, 2250]
    plan.consent["scope"] = "4500_requests"
    with pytest.raises(Incomplete):
        execution.validate(plan.value, plan.consent, plan.checked, cases=cases)


def test_flash_cannot_be_sampled(sample):
    sample.manifests[FLASH]["binding"]["baseline_sample"] = copy.deepcopy(sample.marker)
    with pytest.raises(Incomplete):
        run.validate_campaign(sample.manifests[FLASH], sample.checked, FLASH, cases=sample.cases)


@pytest.mark.parametrize("manifest", [None, {"version": 1}, {"version": 1, "binding": None},
                                     {"version": 1, "binding": []},
                                     {"version": 1, "binding": "baseline_sample"}])
def test_operating_legacy_manifests_refuse_through_validation(tmp_path, manifest):
    from tests.test_eval.test_qualification_operating import population_plan

    plan = population_plan(tmp_path, execution.PRO_COMPARISON)
    execution.validate(plan.value, plan.consent, plan.checked)
    plan.value["models"][PRO]["manifest"] = manifest
    plan.consent["plan_hash"] = digest(plan.value)
    with pytest.raises(Incomplete):
        execution.validate(plan.value, plan.consent, plan.checked)


@pytest.mark.parametrize("legacy_alias", [PRO, FLASH])
@pytest.mark.parametrize("reverse", [False, True])
def test_sample_and_legacy_pair_preserves_completion_contract(sample, legacy_alias, reverse):
    from genesis.eval.qualification import sampling

    states = {a: SimpleNamespace(manifest=m) for a, m in sample.manifests.items()}
    if legacy_alias == PRO:
        states[FLASH].manifest["binding"]["baseline_sample"] = sample.marker
    states[legacy_alias].manifest = None
    if reverse:
        states = dict(reversed(list(states.items())))
    if legacy_alias == PRO:
        with pytest.raises(Incomplete, match="incumbent_sample_population_invalid"):
            sampling.comparison(sample.cases, {}, {}, states, {"unresolved_attempts": 0})
        return
    selected = sampling.campaign_cases(sample.cases, sample.checked, sample.manifests[PRO], PRO)
    records = {PRO: {name: {rep: {c["id"]: {"local": None, "error": None, "agreed": True,
                                           "prediction": c["expected_target"] if name == corpus.NOVELTY
                                           else corpus.label(name, c)} for c in rows}
                            for rep in run.REPETITIONS} for name, rows in selected.items()},
               FLASH: run.unscored(sample.cases, "campaign_incompatible")}
    reports = {PRO: {"status": "baseline_only", "scope": "sampled_incumbent_baseline",
                     "qualification_status": "not_assessed", "baseline_completion": "complete"},
               FLASH: {"status": "incomplete"}}
    if reverse:
        reports, records = (dict(reversed(list(values.items()))) for values in (reports, records))
    result = sampling.comparison(sample.cases, reports, records, states, {"unresolved_attempts": 0})
    assert result["status"] == result["comparison_completion"] == "incomplete"
    assert result["paired"] == result["per_case_comparison"] == {}
    assert result["paired_aliases"] == [PRO, FLASH]


@pytest.mark.parametrize("marker", [None, False, 0, "", [], {}])
def test_present_invalid_marker_never_falls_back_to_unsampled_comparison(sample, marker):
    from genesis.eval.qualification import sampling

    sample.manifests[PRO]["binding"]["baseline_sample"] = marker
    states = {a: SimpleNamespace(manifest=m) for a, m in sample.manifests.items()}
    reports = {a: {"status": "pass"} for a in (PRO, FLASH)}
    records = {a: run.unscored(sample.cases, "unanswered") for a in (PRO, FLASH)}
    with pytest.raises(Incomplete, match="incumbent_sample_invalid"):
        sampling.comparison(sample.cases, reports, records, states, {"unresolved_attempts": 0})


def test_sample_report_never_qualifies_and_observed_model_error_is_complete(sample):
    from genesis.eval.qualification import sampling

    selected = sampling.campaign_cases(sample.cases, sample.checked, sample.manifests[PRO], PRO)
    records = {n: {rep: {c["id"]: {"local": None, "error": None, "prediction": True, "agreed": True}
                         for c in rows} for rep in run.REPETITIONS} for n, rows in selected.items()}
    name = next(iter(records))
    records[name][1]["case-0"].update(error="model_output_invalid", prediction=None, agreed=False)
    result = run.summarize(PRO, selected, records, dict.fromkeys(selected, 0), None)
    sampling.baseline_report(result, selected, records, sample.cases)
    assert result["status"] == "baseline_only" and result["qualification_status"] == "not_assessed"
    assert result["baseline_completion"] == "complete" and result["selected_cases"] == 32
    records[name][1]["case-0"]["local"] = "processing_failed"
    sampling.baseline_report(result, selected, records, sample.cases)
    assert result["baseline_completion"] == "incomplete"


@pytest.mark.parametrize("flash", ["pass", "fail", "incomplete", "unknown"])
@pytest.mark.parametrize("baseline", ["complete", "incomplete", "unknown"])
def test_both_aggregate_readers_have_explicit_sample_status_table(sample, flash, baseline):
    from genesis.eval.qualification import sampling

    reports = {PRO: {"status": "baseline_only", "scope": "sampled_incumbent_baseline",
                     "qualification_status": "not_assessed", "baseline_completion": baseline},
               FLASH: {"status": flash}}
    result = sampling.aggregate_status(reports, {"unresolved_attempts": 0})
    expected = flash if baseline == "complete" and flash in ("pass", "fail") else "incomplete"
    assert result["status"] == expected
    assert result["scope"] == "full_flash_with_sampled_pro_baseline"
    assert result["comparison_completion"] == ("complete" if expected in ("pass", "fail") else "incomplete")


def test_unresolved_financial_evidence_cannot_be_sample_completion(sample):
    from genesis.eval.qualification import sampling

    reports = {PRO: {"status": "baseline_only", "scope": "sampled_incumbent_baseline",
                     "qualification_status": "not_assessed", "baseline_completion": "complete"},
               FLASH: {"status": "pass"}}
    assert sampling.aggregate_status(reports, {"unresolved_attempts": 1})["status"] == "incomplete"


@pytest.mark.parametrize("reverse", [False, True])
def test_paired_subset_uses_full_flash_evidence_and_one_unit_per_case(sample, reverse):
    from genesis.eval.qualification import sampling

    selected = sampling.campaign_cases(sample.cases, sample.checked, sample.manifests[PRO], PRO)
    records = {a: {name: {rep: {c["id"]: {"local": None, "error": None,
                                          "prediction": True, "agreed": True}
                               for c in rows} for rep in run.REPETITIONS}
                   for name, rows in population.items()}
               for a, population in [(PRO, selected), (FLASH, sample.cases)]}
    name = next(iter(sample.cases))
    for rep in run.REPETITIONS:
        records[PRO][name][rep]["case-0"].update(prediction=False, agreed=False)
    reports = {PRO: {"status": "baseline_only", "scope": "sampled_incumbent_baseline",
                     "qualification_status": "not_assessed", "baseline_completion": "complete"},
               FLASH: {"status": "pass"}}
    if reverse:
        reports = dict(reversed(list(reports.items())))
    states = {a: SimpleNamespace(manifest=m) for a, m in sample.manifests.items()}
    result = sampling.comparison(sample.cases, reports, records, states, {"unresolved_attempts": 0})
    assert result["status"] == "pass" and result["comparison_completion"] == "complete"
    assert result["paired_aliases"] == [PRO, FLASH]
    assert all(r["n_cases"] == 4 and "significant" not in r and "recommendation" not in r
               for r in result["per_case_comparison"].values())
    assert all(rep["cases"] == 4 for reps in result["paired"].values() for rep in reps)
    assert all(rep["left_only_correct"] == 0 and rep["right_only_correct"] == 1
               for rep in result["paired"][name])
    # Even evidence outside the paired subset is required for full Flash completion.
    records[FLASH][name][1]["case-5"]["local"] = "unanswered"
    result = sampling.comparison(sample.cases, reports, records, states, {"unresolved_attempts": 0})
    assert result["status"] == result["comparison_completion"] == "incomplete"


def test_class_majorities_distinguish_opposed_effects_with_equal_repetition_marginals():
    from genesis.eval.qualification import sampling

    # Pure report geometry; not an assertion of full reference/transport admission.
    name = next(n for n in corpus.versions() if n not in (corpus.NOVELTY, corpus.RELEVANCE))
    rows = [{"id": f"case-{i}", "user_passed": (4 <= i < 8 or i == 9),
             "reference_provenance": {"label_source": "human"}} for i in range(10)]
    cases, selected = {name: rows}, {name: rows[:8]}
    checked = {"corpus_hash": digest(cases), "plan": {name: {c["id"]: {} for c in rows}}}
    marker = {"purpose": "incumbent_baseline", "corpus_hash": digest(cases),
              "case_ids": {name: [c["id"] for c in rows[:8]]}, "selection_evidence": "synthetic"}
    states = {PRO: SimpleNamespace(manifest={"binding": {"baseline_sample": marker, "admission": checked}}),
              FLASH: SimpleNamespace(manifest={"binding": {}})}
    high = [(1, 1, 0), (1, 0, 1), (0, 1, 1), (0, 0, 0)]
    low = [(1, 1, 1), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    outputs = []
    for reverse in (False, True):
        records, reports = {}, {}
        for alias, population in ((PRO, selected), (FLASH, cases)):
            repetitions = records[alias] = {name: {rep: {} for rep in run.REPETITIONS}}
            for index, case in enumerate(population[name]):
                label = corpus.label(name, case)
                vector = (high if (alias == PRO) == (label != reverse) else low)[index % 4]
                if index >= 8:
                    vector = (1, 1, 1)
                for rep, agreed in enumerate(vector, 1):
                    repetitions[name][rep][case["id"]] = {
                        "agreed": bool(agreed), "prediction": label if agreed else not label,
                        "error": None, "local": None}
            reports[alias] = run.summarize(alias, population, records[alias], {name: 0}, None)
        sampling.baseline_report(reports[PRO], selected, records[PRO], cases)
        result = sampling.comparison(cases, reports, records, states, {"unresolved_attempts": 0})
        stats = result["per_case_comparison"][name]
        assert stats["control_pass_rate"] == stats["treatment_pass_rate"] == 0.5
        assert set(stats["classes"]) == {"false", "true"}
        for label in (False, True):
            group = stats["classes"][str(label).lower()]
            pro_rate = 0.75 if label != reverse else 0.25
            assert group["n_cases"] == 4
            assert group["control_pass_rate"] == pro_rate
            assert group["treatment_pass_rate"] == 1 - pro_rate
            assert not {"significant", "recommendation", "p_value"} & group.keys()
        stats.pop("classes")
        outputs.append(result)
    # The new class breakdown is the only aggregate that distinguishes these histories.
    assert outputs[0] == outputs[1]
