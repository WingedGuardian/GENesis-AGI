"""Operating allocation controls; synthetic consent never authorizes real spending."""
import copy
import hashlib
import json
from contextlib import ExitStack
from decimal import localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.eval.qualification import execution, pinned, run
from genesis.eval.qualification.accounting import Journal, currency
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.qualification_transport_fixtures import bundle


@pytest.fixture
def plan(tmp_path):
    """Complete schedule geometry only, not current production admission proof."""
    data = bundle(tmp_path)
    contract = data.contract
    checked = {"corpus_hash": "a" * 64, "policy_hash": "b" * 64,
               "approval_hash": "c" * 64, "versions": pinned.versions(), "rubrics": [],
               "reference_sources": {}, "production_sources": {},
               "plan": {contract: {f"case-{i}": {"messages": data.messages,
                                               "prompt_hash": digest(data.messages)}
                                    for i in range(750)}},
               "models": {a: pinned.resolve(a).model_id for a in execution.ALIASES},
               "parameters": {a: data.params for a in execution.ALIASES}}
    models = {}
    fingerprint = hashlib.sha256(b"synthetic-only-key").hexdigest()
    for alias in execution.ALIASES:
        identity = {**data.campaign.expected_manifest["binding"]["transports"][data.alias],
                    "model": checked["models"][alias], "receipt_provider": "Synthetic Gateway Display",
                    "provider_identity_evidence": "synthetic mapping only"}
        first = data.campaign.expected_manifest["attempts"][0]
        models[alias] = {"manifest": {"version": 2, "budget": "2.25",
                         "binding": {"admission": run.admission_identity(checked),
                                     "transports": {alias: identity}},
                         "attempts": [{**first, "id": f"a-{i}-{rep}", "alias": alias,
                                       "model": identity["model"], "case_id": f"case-{i}",
                                       "repetition": rep, "max_charge": "0.001",
                                       **{key: identity[key] for key in (
                                           "credential_fingerprint", "receipt_provider",
                                           "provider_identity_evidence")},
                                       "request_hash": digest(pinned.request_body(
                                           identity["model"], data.params, contract, data.messages))}
                                      for i in range(750) for rep in (1, 2, 3)]}}
    value = {"version": 1, "financial_promise": execution.PROMISE,
             "stop_semantics": execution.STOP, "combined_allowance": "5",
             "expected_limit": "5", "baseline_usage": "0",
             "credential_fingerprint": fingerprint,
             "price_snapshot": {"observed_at": "synthetic", "evidence": "synthetic price fixture"},
             "models": models}
    consent = {"version": 1, "plan_hash": digest(value), "scope": "4500_requests",
               "author": "owner", "decision_evidence": "SYNTHETIC TEST ONLY, NOT OWNER CONSENT"}
    return SimpleNamespace(value=value, consent=consent, checked=checked, tmp=tmp_path)


def write_input(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return execution.Input(path)


def select_flash(plan):
    """Reduce synthetic geometry, rebinding admission and full-plan consent."""
    alias = execution.ALIASES[1]
    for key in ("models", "parameters"):
        plan.checked[key] = {alias: plan.checked[key][alias]}
    plan.value["models"] = {alias: plan.value["models"][alias]}
    plan.value["models"][alias]["manifest"]["binding"]["admission"] = run.admission_identity(plan.checked)
    plan.value["stop_semantics"] = execution.SINGLE_STOP
    plan.consent.update(scope="2250_requests", plan_hash=digest(plan.value))
    return plan


def test_plan_binds_every_coordinate_and_allocation(plan):
    frozen = execution.validate(plan.value, plan.consent, plan.checked)
    assert sum(len(m["attempts"]) for m in frozen.values()) == 4500
    assert all(m["binding"]["operating_plan_hash"] == digest(plan.value) for m in frozen.values())
    assert "operating_plan_hash" not in next(iter(plan.value["models"].values()))["manifest"]["binding"]


@pytest.mark.parametrize("change", ["scope", "hash", "author", "evidence", "extra"])
def test_consent_cannot_admit_partial_stale_or_proof_flags(plan, change):
    consent = copy.deepcopy(plan.consent)
    key, value = {"scope": ("scope", "2250_requests"), "hash": ("plan_hash", "0" * 64),
                  "author": ("author", "agent"), "evidence": ("decision_evidence", ""),
                  "extra": ("provider_guaranteed", True)}[change]
    consent[key] = value
    with pytest.raises(Incomplete):
        execution.validate(plan.value, consent, plan.checked)


@pytest.mark.parametrize("key,value", [("combined_allowance", "4"), ("expected_limit", "6"),
                                      ("baseline_usage", "1"), ("expected_limit", 5.0),
                                      ("combined_allowance", "NaN"), ("expected_limit", "0")])
def test_underfunded_or_wider_unapproved_key_envelope_refuses(plan, key, value):
    plan.value[key] = value
    plan.consent["plan_hash"] = digest(plan.value)
    with pytest.raises(Incomplete):
        execution.validate(plan.value, plan.consent, plan.checked)


@pytest.mark.parametrize("change", ["missing", "duplicate", "request", "fingerprint", "zero"])
def test_schedule_must_match_actual_complete_admission(plan, change):
    manifest = plan.value["models"][execution.ALIASES[0]]["manifest"]
    if change == "missing":
        manifest["attempts"].pop()
    elif change == "duplicate":
        manifest["attempts"][-1] = manifest["attempts"][0]
    elif change == "request":
        manifest["attempts"][0]["request_hash"] = "0" * 64
    elif change == "fingerprint":
        manifest["binding"]["transports"][execution.ALIASES[0]]["credential_fingerprint"] = "0" * 64
    else:
        manifest["attempts"][0]["max_charge"] = "0"
    plan.consent["plan_hash"] = digest(plan.value)
    with pytest.raises(Incomplete):
        execution.validate(plan.value, plan.consent, plan.checked)


@pytest.mark.parametrize("replace", [False, True])
def test_input_edit_or_replacement_detected_without_full_hash_per_dispatch(tmp_path, replace):
    path = tmp_path / "plan.json"
    item = write_input(path, {"value": 1})
    try:
        item.check(final=True)
        if replace:
            other = tmp_path / "replacement.json"
            other.write_bytes(path.read_bytes())
            other.chmod(0o600)
            other.replace(path)
        else:
            # Same-tick equal-size writes can share mtime/ctime on real filesystems.
            path.write_text('{"value": 200}')
        with pytest.raises(Incomplete, match="changed"):
            item.check()
    finally:
        item.close()


def test_metadata_collision_preserves_frozen_input_and_final_hash_refuses(tmp_path, monkeypatch):
    path = tmp_path / "plan.json"
    item = write_input(path, {"value": 1})
    try:
        path.write_text('{"value": 2}')
        # Deliberately model the measured equal-size/equal-timestamp collision.
        monkeypatch.setattr(execution, "identity", lambda _info: item.stamp)
        item.check()
        assert item.value == {"value": 1}
        with pytest.raises(Incomplete, match="changed"):
            item.check(final=True)
    finally:
        item.close()


def receipt_row(charge="0.30"):
    spec = {"id": "a", "model": "synthetic", "upstream": "synthetic-provider",
            "receipt_provider": "Synthetic Display", "generation_namespace": "synthetic",
            "request_hash": "f" * 64, "max_charge": "0.20"}
    receipt = {"model": spec["model"], "upstream": spec["upstream"], "attempt": "a",
               "request_hash": spec["request_hash"], "generation_id": "same-generation", "charge": charge}
    return {"spec": spec, "receipts": [receipt, copy.deepcopy(receipt)], "observations": []}


def test_actual_charge_retains_above_allocation_and_deduplicates_receipts():
    row = receipt_row()
    before = copy.deepcopy(row)
    generation, actual = execution.observed_charge(row)
    assert actual == currency("0.30") and generation[-1] == "same-generation"
    assert row == before
    row["receipts"][-1]["charge"] = "0.31"
    assert execution.observed_charge(row) is None


@pytest.mark.parametrize("change", ["limit", "usage", "remaining", "reset"])
async def test_exact_final_key_observation_stops_both_before_reservation(plan, change):
    left = write_input(plan.tmp / "plan.json", plan.value)
    right = write_input(plan.tmp / "consent.json", plan.consent)
    try:
        control = execution.Operating(left, right, plan.checked)
        with Journal(plan.tmp / "mimo", control.manifests[execution.ALIASES[0]]) as a, \
                Journal(plan.tmp / "flash", control.manifests[execution.ALIASES[1]]) as b:
            control.attach(dict(zip(execution.ALIASES, (a, b), strict=True)))
            info = {"limit": "5", "usage": "0", "limit_remaining": "5", "limit_reset": None}
            adapter = SimpleNamespace(fingerprint=plan.value["credential_fingerprint"])
            await control.admission_hook(adapter, "a-0-1", info)
            key, value = {"limit": ("limit", "6"), "usage": ("usage", "0.01"),
                          "remaining": ("limit_remaining", "4"), "reset": ("limit_reset", "daily")}[change]
            info[key] = value
            with pytest.raises(Incomplete):
                await control.admission_hook(adapter, "a-0-1", info)
            assert control.stopped and len(a.lines) == len(b.lines) == 1
            assert control.pending == currency("4.5")
    finally:
        left.close()
        right.close()


def test_shared_cost_counter_ignores_rounding_and_duplicate_generations():
    control = execution.Operating.__new__(execution.Operating)
    control.owners, control.costs, control.observed = {}, {}, currency("0")
    row = receipt_row("0.123456789")
    with localcontext() as context:
        context.prec = 2
        control.update("mimo", "a", row)
        control.update("mimo", "a", row)
        assert control.observed == currency("0.123456789")
        with pytest.raises(Incomplete, match="collision"):
            control.update("flash", "different-attempt", row)
    row["spec"]["generation_namespace"] = "independent-gateway"
    control.update("flash", "different-attempt", row)
    assert control.observed == currency("0.246913578")


@pytest.mark.parametrize("namespace", ["synthetic", "independent-gateway"])
@pytest.mark.parametrize("upstream", ["synthetic-provider", "different-upstream"])
@pytest.mark.parametrize("display", ["Synthetic Display", "Different Display"])
def test_generation_ownership_matches_gateway_namespace_in_counter_and_report(namespace, upstream, display):
    first, second = receipt_row("0.10"), receipt_row("0.10")
    for row in (first, second):
        row["dispatched"] = True
    second["spec"].update(generation_namespace=namespace, upstream=upstream, receipt_provider=display)
    for receipt in second["receipts"]:
        receipt["upstream"] = upstream
    states = {alias: SimpleNamespace(attempts={"a": row}, settled=currency("0.10"),
                                    reserved=currency("0"))
              for alias, row in zip(execution.ALIASES, (first, second), strict=True)}
    report = execution.cost_report(states)
    control = execution.Operating.__new__(execution.Operating)
    control.owners, control.costs, control.observed = {}, {}, currency("0")
    control.update("mimo", "a", first)
    if namespace == "synthetic":
        with pytest.raises(Incomplete, match="collision"):
            control.update("flash", "a", second)
        assert report["observed_receipt_cost"] == "0" and report["unresolved_attempts"] == 2
    else:
        control.update("flash", "a", second)
        assert report["observed_receipt_cost"] == "0.20" and report["unresolved_attempts"] == 0


def test_comparison_lock_releases_and_excludes_other_campaign_orders(tmp_path, monkeypatch):
    (tmp_path / ".genesis").mkdir(mode=0o700)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    fingerprint = "a" * 64
    with (execution.comparison_lock(fingerprint), pytest.raises(BlockingIOError),
          execution.comparison_lock(fingerprint)):
        pytest.fail("second writer admitted")
    with execution.comparison_lock(fingerprint):
        pass


@pytest.mark.parametrize("mode", ["settled", "over_allocation", "usage_discrepancy", "processing_failure"])
@pytest.mark.parametrize("population", ["pair", "flash"])
async def test_live_paired_dispatch_settlement_and_persistent_stop(plan, monkeypatch, mode, population):
    import httpx

    from genesis.eval.qualification.accounting import total
    from tests.test_eval.qualification_transport_fixtures import Gateway, ask

    if population == "flash":
        select_flash(plan)
    aliases = execution.selected_aliases(plan.value["models"])

    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    secrets = plan.tmp / "secrets.env"
    secrets.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)

    class AccountingGateway(Gateway):
        def __call__(self, request):
            if request.url.path.endswith("/key"):
                self.key_reads += 1
                usage = total(currency("0.0001") for _ in self.requests)
                if mode == "usage_discrepancy" and self.requests:
                    usage = currency("0.01")
                remaining = total([currency("5"), usage.copy_negate()])
                return httpx.Response(200, content=(
                    '{"data":{"limit":5,"usage":' + str(usage)
                    + ',"limit_remaining":' + str(remaining) + ',"limit_reset":null}}'
                ).encode())
            response = super().__call__(request)
            if mode == "over_allocation" and request.method == "POST":
                body = response.json()
                body["usage"]["cost"] = 0.003
                self.generations[body["id"]]["total_cost"] = 0.003
                return httpx.Response(200, json=body)
            return response

    gateway = AccountingGateway()
    left = write_input(plan.tmp / "plan.json", plan.value)
    right = write_input(plan.tmp / "consent.json", plan.consent)
    try:
        control = execution.Operating(left, right, plan.checked)
        with ExitStack() as stack:
            journals = {alias: stack.enter_context(Journal(plan.tmp / alias, control.manifests[alias]))
                        for alias in aliases}
            a = journals[aliases[0]]
            control.attach(journals)
            adapters = [control.router(j, alias=alias, params=plan.checked["parameters"][alias],
                                       dispatch=True, transport=gateway.transport)
                        for alias, j in journals.items()]
            contract, rows = next(iter(plan.checked["plan"].items()))
            messages = rows["case-0"]["messages"]
            for adapter in adapters:
                adapter.bind(contract, 1, case_id="case-0")
            if mode in ("over_allocation", "usage_discrepancy"):
                with pytest.raises(Incomplete):
                    await ask(adapters[0], messages, "case-0")
            else:
                assert (await ask(adapters[0], messages, "case-0")).success
                if mode == "processing_failure":
                    adapters[0].stop_processing("case-0", RuntimeError("synthetic local error"))
            if mode == "settled":
                adapter = adapters[-1]
                adapter.bind(contract, 1, case_id="case-1" if population == "flash" else "case-0")
                assert (await ask(adapter, messages, "case-1" if population == "flash" else "case-0")).success
                assert len(gateway.requests) == 2 and control.observed == currency("0.0002")
                # Replaying a settled coordinate performs no POST or credential GET.
                reads = gateway.key_reads
                adapters[0].bind(contract, 1, case_id="case-0")
                assert (await ask(adapters[0], messages, "case-0")).success
                assert len(gateway.requests) == 2 and gateway.key_reads == reads
            else:
                reads = gateway.key_reads
                with pytest.raises(Incomplete):
                    if population == "flash":
                        adapters[0].bind(contract, 1, case_id="case-1")
                    await ask(adapters[-1], messages, "case-1" if population == "flash" else "case-0")
                assert len(gateway.requests) == 1 and gateway.key_reads == reads
                assert control.stopped
                assert any(line["kind"] == "failure" for line in a.lines)
            financial = control.report()
            if mode == "over_allocation":
                assert financial["observed_receipt_cost"] == "0.003"
                assert financial["over_allocation_attempts"] == 1
                assert financial["journal_accounting"][aliases[0]]["journal_settled"] == "0"
            frozen = copy.deepcopy(control.manifests)
        if mode != "settled":
            with (Journal(plan.tmp / aliases[0], frozen[aliases[0]]) as reopened,
                  pytest.raises(Incomplete)):
                reopened.ready()
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("invalid", ["model", "conflicting_cost", "missing_cost"])
@pytest.mark.parametrize("namespace", ["synthetic", "other-gateway"])
@pytest.mark.parametrize("upstream", ["synthetic-provider", "other-upstream"])
@pytest.mark.parametrize("shared_id", [True, False])
def test_unresolved_receipts_still_own_their_generation(invalid, namespace, upstream, shared_id):
    first, second = receipt_row("0.10"), receipt_row("0.10")
    for row in (first, second):
        row["dispatched"] = True
    if invalid == "model":
        first["receipts"][0]["model"] = "contradictory-model"
    elif invalid == "conflicting_cost":
        first["receipts"][0]["charge"] = "0.11"
    else:
        for receipt in first["receipts"]:
            receipt.pop("charge")
    second["spec"].update(generation_namespace=namespace, upstream=upstream)
    for receipt in second["receipts"]:
        receipt.update(upstream=upstream,
                       generation_id="same-generation" if shared_id else "independent-id")
    states = {alias: SimpleNamespace(attempts={"a": row}, settled=currency("0"), reserved=currency("0.20"))
              for alias, row in zip(execution.ALIASES, (first, second), strict=True)}
    report = execution.cost_report(states)
    collision = namespace == "synthetic" and shared_id
    assert report["observed_receipt_cost"] == ("0" if collision else "0.10")
    assert report["unresolved_attempts"] == (2 if collision else 1)


@pytest.mark.parametrize("shared", ["body", "header", "neither"])
@pytest.mark.parametrize("namespace", ["synthetic", "other-gateway"])
@pytest.mark.parametrize("retain_receipt", [True, False])
def test_transport_conflicting_identity_facts_still_own_generations(shared, namespace, retain_receipt):
    first, second = receipt_row("0.10"), receipt_row("0.10")
    for row in (first, second):
        row["dispatched"] = True
    # Use the real transport representation, including its contradictory list ID.
    observation = pinned._response_identity({"id": "body-id"}, {"x-generation-id": "header-id"})
    first["observations"] = [observation, copy.deepcopy(observation)]
    first["receipts"] = [copy.deepcopy(observation)] if retain_receipt else []
    second["spec"]["generation_namespace"] = namespace
    generation = {"body": "body-id", "header": "header-id", "neither": "unclaimed-id"}[shared]
    for receipt in second["receipts"]:
        receipt["generation_id"] = generation
    states = {alias: SimpleNamespace(attempts={"a": row}, settled=currency("0"), reserved=currency("0.20"))
              for alias, row in zip(execution.ALIASES, (first, second), strict=True)}
    before = copy.deepcopy(states)
    report = execution.cost_report(states)
    collision = namespace == "synthetic" and shared != "neither"
    assert report["observed_receipt_cost"] == ("0" if collision else "0.10")
    assert report["unresolved_attempts"] == (2 if collision else 1)
    assert states == before
