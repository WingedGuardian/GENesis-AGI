"""Native calculated expenses; all responses, identities and prices are synthetic."""

import copy
import json
from decimal import Decimal, localcontext

import pytest

from genesis.eval.qualification import accounting, execution, native_expense
from genesis.eval.qualification.evidence import Incomplete, digest


def fixture(maximum="0.001"):
    tariff = {
        "version": 1, "currency": "USD", "unit_tokens": 1000000,
        "provider": "deepseek", "model": "deepseek-flash",
        "selection": "published_tier_interval",
        "rates": {
            "off_peak": {"prompt_cache_hit": "0.003", "prompt_cache_miss": "0.15", "completion": "0.6"},
            "peak": {"prompt_cache_hit": "0.006", "prompt_cache_miss": "0.30", "completion": "1.20"},
        },
        "pricing_evidence": {"url": "https://api-docs.deepseek.com/quick_start/pricing/",
                             "sha256": "a" * 64, "observed_at": "2026-01-01T00:00:00Z"},
    }
    spec = dict(id="synthetic-1", alias="deepseek-flash", model="deepseek-flash",
                upstream="deepseek", endpoint="https://api.deepseek.com/chat/completions",
                request_hash="b" * 64, max_charge=maximum, generation_namespace="deepseek",
                cost_basis="native_tariff_v1", native_tariff=tariff)
    usage = dict(prompt_tokens=96, completion_tokens=10, total_tokens=106,
                 prompt_cache_hit_tokens=64, prompt_cache_miss_tokens=32)
    body = dict(id="synthetic-generation", object="chat.completion", model=spec["model"],
                usage=usage, choices=[{"message": {"role": "assistant", "content": "synthetic"},
                                      "finish_reason": "stop"}])
    observation = dict(model=spec["model"], upstream=spec["upstream"], request_hash=spec["request_hash"],
                       request_url=spec["endpoint"], response_url=spec["endpoint"],
                       generation_id=body["id"], http_status=200, content="synthetic",
                       native_usage=copy.deepcopy(usage), raw_body=json.dumps(body))
    return spec, observation


def row_for(spec, observation, receipts=None):
    return dict(spec=spec, observations=[observation], receipts=receipts or [], lookups=[], dispatched=True)


def history(spec, observation, receipt=None):
    manifest = dict(version=2, budget=spec["max_charge"], binding={"source": "synthetic"}, attempts=[spec])
    lines = [dict(kind="manifest", manifest=manifest),
             dict(kind="reservation", attempt=spec["id"], amount=spec["max_charge"]),
             dict(kind="dispatch", attempt=spec["id"], request_hash=spec["request_hash"]),
             dict(kind="observation", attempt=spec["id"], evidence=observation)]
    if receipt is not None:
        lines.append(dict(kind="billing", attempt=spec["id"], receipt=receipt))
    return lines


@pytest.mark.parametrize("hit,miss,out,lower,upper", [
    (64, 32, 10, "0.000010992", "0.000021984"), (0, 100, 20, "0.000027", "0.000054"),
    (100, 0, 20, "0.0000123", "0.0000246"), (0, 0, 0, "0", "0"),
])
def test_exact_intervals_survive_low_global_precision(hit, miss, out, lower, upper):
    spec, observation = fixture()
    usage = dict(prompt_tokens=hit + miss, completion_tokens=out, total_tokens=hit + miss + out,
                 prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=miss)
    raw = json.loads(observation["raw_body"])
    raw["usage"] = {**usage, "cost": "999", "prompt_tokens_details": {"cached_tokens": hit}}
    observation.update(native_usage=usage, raw_body=json.dumps(raw))
    before = copy.deepcopy(observation)
    with localcontext() as context:
        context.prec = 2
        assert native_expense.observation_interval(spec, observation) == (Decimal(lower), Decimal(upper))
    assert observation == before


def test_tiny_tariffs_are_never_rounded_to_a_currency_quantum():
    spec, observation = fixture()
    for tier in spec["native_tariff"]["rates"].values():
        tier.update(dict.fromkeys(tier, "0.000000000000000000000000000003"))
    with localcontext() as context:
        context.prec = 2
        assert native_expense.observation_interval(spec, observation) == (Decimal("3.18E-34"),) * 2


@pytest.mark.parametrize("field", list(fixture()[1]["native_usage"]))
@pytest.mark.parametrize("invalid", [None, True, False, -1, 1.5, "1"])
def test_native_usage_requires_all_strict_nonnegative_integers(field, invalid):
    spec, observation = fixture()
    raw = json.loads(observation["raw_body"])
    for usage in (observation["native_usage"], raw["usage"]):
        if invalid is None:
            usage.pop(field)
        else:
            usage[field] = invalid
    observation["raw_body"] = json.dumps(raw)
    with pytest.raises(Incomplete):
        native_expense.observation_interval(spec, observation)


@pytest.mark.parametrize("invalid", [True, 1.0])
def test_consistent_token_sums_still_reject_noninteger_count_types(invalid):
    spec, observation = fixture()
    usage = dict(prompt_tokens=1, completion_tokens=0, total_tokens=1,
                 prompt_cache_hit_tokens=invalid, prompt_cache_miss_tokens=0)
    raw = json.loads(observation["raw_body"])
    raw["usage"] = usage
    observation.update(native_usage=usage, raw_body=json.dumps(raw))
    with pytest.raises(Incomplete, match="token counts"):
        native_expense.observation_interval(spec, observation)


@pytest.mark.parametrize("change", [
    {"total_tokens": 107}, {"prompt_cache_miss_tokens": 33},
    {"prompt_tokens_details": {"cached_tokens": True}}, {"prompt_tokens_details": {"cached_tokens": 63}},
    {"completion_tokens_details": {"reasoning_tokens": 11}}, {"server_tool_use": {"web_search_requests": 1}},
])
def test_raw_usage_consistency_and_unsupported_charges(change):
    spec, observation = fixture()
    raw = json.loads(observation["raw_body"])
    raw["usage"].update(change)
    observation["raw_body"] = json.dumps(raw)
    with pytest.raises(Incomplete):
        native_expense.observation_interval(spec, observation)


@pytest.mark.parametrize("path,value", [
    (("version",), True), (("version",), 2), (("unit_tokens",), True), (("currency",), "EUR"),
    (("unexpected",), 1), (("rates", "peak", "completion"), "0.1"),
    (("rates", "off_peak", "completion"), "0"), (("pricing_evidence", "sha256"), "invalid"),
])
def test_closed_tariff_schema_and_manifest_admission(path, value):
    spec, _ = fixture()
    target = spec["native_tariff"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(Incomplete):
        native_expense.validate_spec(spec)
    with pytest.raises(Incomplete):
        accounting.validate_manifest(history(spec, {})[0]["manifest"])


@pytest.mark.parametrize("field", ["cost_basis", "attempt", "request_hash", "generation_id", "model",
                                  "upstream", "observation_sha256", "tariff_sha256",
                                  "calculated_lower", "calculated_upper", "unexpected"])
def test_ten_field_evidence_is_bound_and_recomputed(field):
    spec, observation = fixture()
    receipt = native_expense.billing_evidence(spec, observation)
    assert set(receipt) == {"cost_basis", "attempt", "request_hash", "generation_id", "model", "upstream",
                            "observation_sha256", "tariff_sha256", "calculated_lower", "calculated_upper"}
    assert receipt["observation_sha256"] == digest(observation)
    assert receipt["tariff_sha256"] == digest(spec["native_tariff"])
    assert native_expense.interval(row_for(spec, observation, [receipt, copy.deepcopy(receipt)])) == (
        Decimal("0.000010992"), Decimal("0.000021984"))
    receipt[field] = "0.000099" if field.startswith("calculated_") else "wrong"
    assert native_expense.interval(row_for(spec, observation, [receipt])) is None


@pytest.mark.parametrize("phase", [1, 2, 3, 4, 5])
def test_crash_replay_never_fabricates_evidence_or_releases_missing_cost(tmp_path, phase):
    spec, observation = fixture(maximum="0.000021984")
    receipt = native_expense.billing_evidence(spec, observation)
    lines = history(spec, observation, receipt)
    path = tmp_path / "native-campaign"
    with accounting.Journal(path, lines[0]["manifest"]) as journal:
        for line in lines[1:phase]:
            journal.append(line["kind"], **{k: v for k, v in line.items() if k != "kind"})
    saved = (path / "answers.jsonl").read_bytes()
    with accounting.Journal(path, lines[0]["manifest"]) as journal:
        assert journal.state.settled == (Decimal(spec["max_charge"]) if phase == 5 else 0)
        assert journal.state.reserved == (Decimal(spec["max_charge"]) if 1 < phase < 5 else 0)
        if phase > 1:
            with pytest.raises(Incomplete):
                journal.dispatch(spec["id"])
    assert (path / "answers.jsonl").read_bytes() == saved


def test_above_allocation_and_authority_split_preserve_legacy_liability():
    spec, observation = fixture(maximum="0.00002")
    native = accounting.reconstruct(history(spec, observation, native_expense.billing_evidence(spec, observation)))
    legacy_spec = {k: v for k, v in spec.items() if k not in {"cost_basis", "native_tariff"}}
    legacy_spec.update(generation_namespace="openrouter", max_charge="0.001")
    legacy_obs = dict(model=spec["model"], upstream=spec["upstream"], generation_id="legacy-synthetic",
                      content="synthetic", usage={"cost": "0.0000617902504"})
    legacy_receipt = dict(model=spec["model"], upstream=spec["upstream"], generation_id="legacy-synthetic", charge="0.00006179")
    legacy = accounting.reconstruct(history(legacy_spec, legacy_obs, legacy_receipt))
    assert native.reserved == Decimal("0.00002") and legacy.reserved == Decimal("0.001")
    assert execution.observed_charge(native.attempts[spec["id"]]) is None
    injected = {**native_expense.billing_evidence(spec, observation), "charge": "0.00001"}
    assert accounting._billing(row_for(legacy_spec, observation, [injected])) is None
    report = execution.cost_report({"native": native, "legacy": legacy})
    assert Decimal(report["observed_receipt_cost"]) == 0
    assert Decimal(report["calculated_cost_interval"]["upper"]) == Decimal("0.000021984")
    assert report["native_invoice_cost_status"] == "unobserved" and report["over_allocation_attempts"] == 1
    assert report["journal_accounting"]["native"]["journal_settled_basis"] == "native_tariff_v1_upper"


def test_valid_native_cost_is_resolved_without_a_provider_invoice_and_collisions_refuse():
    spec, observation = fixture()
    raw = json.loads(observation["raw_body"])
    raw["usage"]["cost"] = "999"
    observation["raw_body"] = json.dumps(raw)
    receipt = native_expense.billing_evidence(spec, observation)
    state = accounting.reconstruct(history(spec, observation, receipt))
    report = execution.cost_report({"native": state})
    assert report["unresolved_attempts"] == 0
    assert report["native_uncontracted_usage_cost_attempts"] == 1
    assert report["calculated_cost_interval"] == dict(basis="native_tariff_v1", currency="USD",
        lower="0.000010992", upper="0.000021984", resolved_attempts=1)
    assert Decimal(report["observed_receipt_cost"]) == 0
    legacy_spec = {k: v for k, v in spec.items() if k not in {"cost_basis", "native_tariff"}}
    legacy_obs = {**observation, "usage": {"cost": "0.00001"}}
    legacy_receipt = dict(model=spec["model"], upstream=spec["upstream"],
                          generation_id=observation["generation_id"], charge="0.00001")
    legacy = accounting.reconstruct(history(legacy_spec, legacy_obs, legacy_receipt))
    report = execution.cost_report({"native": state, "legacy": legacy})
    assert report["unresolved_attempts"] == 2
    assert report["calculated_cost_interval"]["resolved_attempts"] == 0
    assert Decimal(report["observed_receipt_cost"]) == 0


@pytest.mark.parametrize("field,value", [
    ("request_url", "https://synthetic.invalid"), ("response_url", "https://synthetic.invalid"),
    ("request_hash", "c" * 64), ("generation_id", "different"), ("model", "different"),
    ("http_status", True), ("http_status", 500), ("native_usage", {}),
])
def test_native_origin_and_generation_must_match_before_money_is_derived(field, value):
    spec, observation = fixture()
    observation[field] = value
    with pytest.raises(Incomplete):
        native_expense.observation_interval(spec, observation)


def test_unsupported_lookup_and_conflicting_duplicate_never_resolve():
    spec, observation = fixture()
    receipt = native_expense.billing_evidence(spec, observation)
    row = row_for(spec, observation, [receipt])
    row["lookups"] = [{"generation_id": "different"}]
    assert native_expense.interval(row) is None
    row["lookups"] = []
    row["receipts"].append({**receipt, "calculated_upper": "0.0001"})
    assert native_expense.interval(row) is None


def test_valid_financial_usage_survives_local_answer_failure():
    spec, observation = fixture()
    observation.update(content=None, finish_reason="refusal", error="synthetic local answer failure")
    assert native_expense.observation_interval(spec, observation) == (Decimal("0.000010992"), Decimal("0.000021984"))


def test_native_namespace_is_part_of_the_expense_authority():
    spec, _ = fixture()
    spec["generation_namespace"] = "openrouter"
    with pytest.raises(Incomplete):
        native_expense.validate_spec(spec)


def test_widely_separated_rates_remain_exact_under_low_context():
    spec, observation = fixture()
    for tier in spec["native_tariff"]["rates"].values():
        tier.update(prompt_cache_hit="1" + "0" * 100,
                    prompt_cache_miss="0." + "0" * 99 + "1", completion="0." + "0" * 99 + "1")
    expected = Decimal("64" + "0" * 198 + "42" + "e-106")
    with localcontext() as context:
        context.prec = 2
        assert native_expense.observation_interval(spec, observation) == (expected, expected)


def test_invalid_raw_json_is_an_incomplete_financial_observation():
    spec, observation = fixture()
    observation["raw_body"] = "{"
    with pytest.raises(Incomplete):
        native_expense.observation_interval(spec, observation)


@pytest.mark.parametrize("invalid_raw", [
    '{"id":"a","id":"b"}', '{"usage":{"cost":NaN}}', '{"usage":{"cost":1e999}}',
])
def test_native_raw_json_keeps_the_shared_strict_decoder(invalid_raw):
    spec, observation = fixture()
    observation["raw_body"] = invalid_raw
    with pytest.raises(Incomplete):
        native_expense.observation_interval(spec, observation)


def test_raw_error_envelope_is_not_a_financial_success():
    spec, observation = fixture()
    raw = json.loads(observation["raw_body"])
    raw["error"] = {}
    observation["raw_body"] = json.dumps(raw)
    with pytest.raises(Incomplete):
        native_expense.observation_interval(spec, observation)


def assert_claim_accounting(spec, observation, resolved):
    try:
        receipt = native_expense.billing_evidence(spec, observation)
    except Incomplete:
        receipt = None
    state = accounting.reconstruct(history(spec, observation, receipt))
    report = execution.cost_report({"native": state})
    assert state.reserved == (0 if resolved else Decimal(spec["max_charge"]))
    assert state.settled == (Decimal("0.000021984") if resolved else 0)
    assert report["unresolved_attempts"] == int(not resolved)
    assert report["calculated_cost_interval"]["resolved_attempts"] == int(resolved)
    assert Decimal(report["observed_receipt_cost"]) == 0


@pytest.mark.parametrize("field", ["body_generation_id", "header_generation_id", "response_generation_id",
                                  "response_model", "response_provider", "response_headers"])
@pytest.mark.parametrize("variant", ["absent", "none", "agree", "conflict", "bool", "number", "container", "blank"])
def test_all_retained_identity_claims_control_financial_settlement(field, variant):
    spec, observation = fixture()
    expected = {"response_model": spec["model"], "response_provider": spec["upstream"]}.get(
        field, observation["generation_id"])
    values = {"none": None, "agree": expected, "conflict": "contradictory", "bool": True,
              "number": 1, "container": [], "blank": ""}
    if variant != "absent":
        observation[field] = ({"x-generation-id": values[variant]} if field == "response_headers"
                              else values[variant])
    assert_claim_accounting(spec, observation, variant in {"absent", "none", "agree"})


@pytest.mark.parametrize("headers", [None, "", [], 1, True])
def test_malformed_retained_header_containers_hold_the_whole_reservation(headers):
    spec, observation = fixture()
    observation["response_headers"] = headers
    assert_claim_accounting(spec, observation, False)


@pytest.mark.parametrize("source", ["native_observation", "native_raw_body", "native_header", "legacy_receipt"])
def test_returned_generation_claim_marks_every_owner_unresolved_without_mutating_counters(source):
    spec, second_observation = fixture()
    first_observation = copy.deepcopy(second_observation)
    first_observation["generation_id"] = "synthetic-first-generation"
    raw = json.loads(first_observation["raw_body"])
    raw["id"] = first_observation["generation_id"]
    first_observation["raw_body"] = json.dumps(raw)
    claimed = second_observation["generation_id"]
    if source.startswith("native"):
        if source == "native_raw_body":
            raw["id"] = claimed
            first_observation["raw_body"] = json.dumps(raw)
        elif source == "native_header":
            first_observation["response_headers"] = {"x-generation-id": claimed}
        else:
            first_observation["response_generation_id"] = claimed
        with pytest.raises(Incomplete):
            native_expense.billing_evidence(spec, first_observation)
        first_receipt = None
        second_receipt = native_expense.billing_evidence(spec, second_observation)
    else:
        spec = {k: v for k, v in spec.items() if k not in {"cost_basis", "native_tariff"}}
        spec["generation_namespace"] = "openrouter"
        for observed in (first_observation, second_observation):
            observed["usage"] = {"cost": "0.00001"}
        # The actual GET error path retains its returned ID before adding model/charge.
        first_receipt = dict(generation_id=first_observation["generation_id"],
                             response_generation_id=claimed, attempt=spec["id"],
                             request_hash=spec["request_hash"], http_status=200,
                             error="synthetic billing GET identity mismatch")
        second_receipt = dict(generation_id=claimed, model=spec["model"],
                              upstream=spec["upstream"], charge="0.00001")
    first = accounting.reconstruct(history(spec, first_observation, first_receipt))
    second = accounting.reconstruct(history(spec, second_observation, second_receipt))
    states = {"first": first, "second": second}
    before = copy.deepcopy(states)
    report = execution.cost_report(states)
    assert first.reserved == Decimal(spec["max_charge"]) and second.reserved == 0
    assert report["unresolved_attempts"] == 2
    assert Decimal(report["observed_receipt_cost"]) == 0
    if source.startswith("native"):
        assert report["calculated_cost_interval"]["resolved_attempts"] == 0
    assert states == before


@pytest.mark.parametrize("timestamp", ["not-a-time", "2026-01-01", "2026-01-01T00:00:00",
                                       "2026-01-01T00:00:00-05:00", "2026-01-01T00:00:00+00:01"])
def test_pricing_evidence_requires_a_valid_utc_timestamp(timestamp):
    spec, _ = fixture()
    spec["native_tariff"]["pricing_evidence"]["observed_at"] = timestamp
    with pytest.raises(Incomplete):
        accounting.validate_manifest(history(spec, {})[0]["manifest"])


@pytest.mark.parametrize("timestamp", ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00",
                                       "2026-01-01T00:00:00.123456Z"])
def test_pricing_evidence_preserves_valid_utc_timestamp(timestamp):
    spec, observation = fixture()
    spec["native_tariff"]["pricing_evidence"]["observed_at"] = timestamp
    before = copy.deepcopy(spec)
    accounting.validate_manifest(history(spec, {})[0]["manifest"])
    assert_claim_accounting(spec, observation, True)
    assert spec == before


@pytest.mark.parametrize("field", ["generation_id", "body_generation_id", "header_generation_id",
                                  "response_generation_id", "raw_body", "response_headers"])
def test_study_claims_every_retained_native_generation(field):
    from genesis.eval.qualification.study_report import Study

    spec, observation = fixture()
    claim = "synthetic-other-generation"
    if field == "raw_body":
        raw = json.loads(observation["raw_body"])
        raw["id"] = claim
        observation[field] = json.dumps(raw)
    elif field == "response_headers":
        observation[field] = {"x-generation-id": claim}
    else:
        observation[field] = claim
    study = object.__new__(Study)
    study.owners = {(spec["generation_namespace"], claim): ("other", spec["id"])}
    row = row_for(spec, observation)
    before = copy.deepcopy(row)
    with pytest.raises(Incomplete, match="study_generation_collision"):
        study._claim(("current", spec["id"]), row)
    assert row == before
