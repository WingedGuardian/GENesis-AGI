"""Qualification scoring and private reports; no inferred paid eligibility."""

import json
from copy import deepcopy
from pathlib import Path

from genesis.eval.qualification import contracts, corpus, pinned, preflight
from genesis.eval.qualification.accounting import reconstruct, validate_manifest
from genesis.eval.qualification.evidence import Campaign, Incomplete, canonical, digest
from genesis.eval.scorers import LLMJudgeScorer

REPETITIONS = (1, 2, 3)
GATE = 0.8
# Every repetition must pass; a decided failure cannot invite another paid run.
DECIDED = ("fail", "incomplete", "pass")
# Independent routes retain their own verdict while the overall result names work left.
OVERALL = ("incomplete", "fail", "pass")


class ReadView:
    """One private snapshot; local processing failures never append to history."""

    def __init__(self, directory):
        snapshot = Campaign.read(Path(directory))
        self.lines, self.torn_tail = snapshot.lines, snapshot.torn_tail
        self._derived = reconstruct(self.lines)
        self._failed = False

    @property
    def state(self):
        return deepcopy(self._derived)

    def attempt(self, identity):
        return deepcopy(self._derived.attempts[identity])

    def ready(self):
        if self.torn_tail or self._derived.blockers or self._failed:
            raise Incomplete("campaign_incomplete")

    def fail(self, _identity, _error):
        self._failed = True


def admission_identity(checked):
    """Compare shared grading identity, independently of the candidate model."""
    shared = {name: checked[name] for name in (
        "corpus_hash", "policy_hash", "approval_hash", "versions", "rubrics",
        "reference_sources", "production_sources",
    )}
    shared["plan"] = {
        name: {identity: {key: value for key, value in row.items() if key != "storage"}
               for identity, row in rows.items()}
        for name, rows in checked["plan"].items()
    }
    shared["runner_sources"] = {
        name: preflight.file_hash(Path(__file__).with_name(name))
        for name in ("run.py", "__main__.py")
    }
    return deepcopy(shared)


def validate_campaign(manifest, checked, alias):
    """All frozen coordinates and wire bodies must match complete current admission."""
    validate_manifest(manifest)
    binding = manifest["binding"]
    if canonical(binding.get("admission")) != canonical(admission_identity(checked)):
        raise Incomplete("campaign_binding_changed")
    transports = binding.get("transports")
    if not isinstance(transports, dict) or set(transports) != {alias}:
        raise Incomplete("campaign_transport_mismatch")
    transport = transports[alias]
    params, model = checked["parameters"][alias], checked["models"][alias]
    required = {
        "model": model, "params": params, "endpoint": pinned.ENDPOINT,
        "generation_namespace": "openrouter",
    }
    if not isinstance(transport, dict) or any(
        canonical(transport.get(key)) != canonical(value) for key, value in required.items()
    ):
        raise Incomplete("campaign_transport_mismatch")
    fingerprint = transport.get("credential_fingerprint")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
        or any(not isinstance(transport.get(key), str) or not transport[key].strip()
               for key in ("receipt_provider", "provider_identity_evidence"))):
        raise Incomplete("campaign_identity_missing")
    expected = {}
    for contract, rows in checked["plan"].items():
        for identity, row in rows.items():
            for repetition in REPETITIONS:
                coordinate = contract, checked["versions"][contract], identity, repetition, row["prompt_hash"]
                expected[coordinate] = digest(pinned.request_body(model, params, contract, row["messages"]))
    actual = {}
    fields = {
        "alias": alias, "model": model, "upstream": params["upstream"],
        "endpoint": pinned.ENDPOINT + "/chat/completions", "generation_namespace": "openrouter",
        **{key: transport[key] for key in (
            "credential_fingerprint", "receipt_provider", "provider_identity_evidence"
        )},
    }
    for row in manifest["attempts"]:
        coordinate = tuple(row.get(key) for key in (
            "contract", "version", "case_id", "repetition", "prompt_hash"
        ))
        if (any(not isinstance(coordinate[index], str) for index in (0, 1, 2, 4))
            or type(row.get("repetition")) is not int or coordinate in actual
            or any(canonical(row.get(key)) != canonical(value) for key, value in fields.items())):
            raise Incomplete("campaign_schedule_mismatch")
        actual[coordinate] = row["request_hash"]
    if canonical(sorted(actual.items())) != canonical(sorted(expected.items())):
        raise Incomplete("campaign_schedule_mismatch")


def _paid_eligibility(*_args, **_kwargs):
    """No real provider bound has been verified; no public bypass is offered."""
    raise Incomplete("financial_eligibility_unavailable")


def worst(statuses, order=DECIDED):
    statuses = set(statuses)
    return next((status for status in order if status in statuses), "incomplete")


def _record(name, case, result=None, local=None):
    record = {"prediction": None, "error": None, "local": local}
    if local is None:
        record.update(result)
    record["agreed"] = (
        local is None and record["error"] is None
        and record["prediction"] == corpus.expected(name, case)
    )
    return record


def _clamped(name, content):
    if name == corpus.NOVELTY:
        return False
    key = "relevance" if name == corpus.RELEVANCE else "score"
    raw = contracts.raw_score(content, key, rubric=name != corpus.RELEVANCE)
    return raw is not None and not 0 <= raw <= 1


def gate(name, cases, rows):
    floor = corpus.FLOORS.get(name, corpus.DEFAULT_FLOOR)
    classes = {}
    for cls in (False, True):
        ids = [case["id"] for case in cases if corpus.label(name, case) is cls]
        correct = sum(rows[identity]["agreed"] for identity in ids)
        classes[str(cls).lower()] = {
            "denominator": len(ids), "correct": correct,
            "agreement": correct / len(ids) if ids else None,
        }
    unscored = [identity for identity, row in rows.items() if row["local"]]
    errors = sum(bool(row["error"]) for row in rows.values())
    correct = sum(row["agreed"] for row in rows.values())
    if unscored or any(classes[str(cls).lower()]["denominator"] < n for cls, n in floor.items()):
        status = "incomplete"
    elif errors or any(value["agreement"] < GATE for value in classes.values()) or correct / len(rows) < GATE:
        status = "fail"
    else:
        status = "pass"
    if name == corpus.NOVELTY and any(
        row["prediction"] is not None and not row["agreed"] for row in rows.values()
    ):
        status = "fail"
    return {
        "status": status, "denominator": len(rows), "correct": correct,
        "errors": errors, "unscored": len(unscored),
        "unscored_reasons": sorted({rows[identity]["local"] for identity in unscored}),
        "classes": classes,
    }


async def _case(name, case, router, sandbox):
    """Use each actual production contract; inspect only a verified served answer."""
    if name == corpus.NOVELTY:
        result = await contracts.novelty(case, router, sandbox)
    elif name == corpus.RELEVANCE:
        result = await contracts.relevance(case, router)
    else:
        prediction, _, detail = await LLMJudgeScorer(router=router).score_async(
            case["actual"], case.get("expected", ""), case["scorer_config"]
        )
        error = json.loads(detail).get("error")
        result = {"prediction": None if error else prediction, "error": error}
    if router.failure or case["id"] not in router.served or len(router.calls) != 1:
        raise Incomplete("answer_not_verified")
    messages, content = router.calls[0]
    if name != corpus.NOVELTY:
        key = "relevance" if name == corpus.RELEVANCE else "score"
        if contracts.raw_score(content, key, rubric=name != corpus.RELEVANCE) is None:
            result = {"prediction": None, "error": "model_output_invalid"}
    if result["error"]:
        return {"prediction": None, "error": "model_output_invalid"}
    if name == corpus.NOVELTY:
        from genesis.eval.qualification.storage import replay

        mapping = contracts.candidate_mapping(case, messages, router.candidate_ids)
        result["storage"] = await replay(
            case, messages, mapping, content, sandbox, router.alias, router.cfg.model_id
        )
    return result


async def score(cases, router, temp_root):
    """Serial orchestration. A local fault stops subsequent acquisition, not just scoring."""
    records, clamped, stopped = {}, {}, False
    attempts = {coordinate[:4]: attempt for coordinate, attempt in router.schedule.items()}
    versions = corpus.versions()
    async with contracts.Sandbox(temp_root) as sandbox:
        for name, items in cases.items():
            records[name], clamped[name] = {}, 0
            for repetition in REPETITIONS:
                rows = records[name][repetition] = {}
                for case in items:
                    identity = case["id"]
                    router.bind(name, repetition, case_id=identity)
                    if stopped:
                        rows[identity] = _record(name, case, local="processing_stopped")
                        continue
                    try:
                        router.campaign.ready()
                        if not router.dispatch:
                            # Missing answers in an otherwise valid snapshot are reportable.
                            attempt = attempts[name, versions[name], identity, repetition]
                            if not router.campaign.attempt(attempt)["dispatched"]:
                                rows[identity] = _record(name, case, local="unanswered")
                                continue
                        result = await _case(name, case, router, sandbox)
                    except BaseException as exc:
                        stopped = True
                        try:
                            router.stop_processing(identity, exc)
                        except BaseException as append_error:
                            exc.add_note("failure evidence append failed: " + type(append_error).__name__)
                        if not isinstance(exc, Exception):
                            raise
                        rows[identity] = _record(name, case, local="processing_failed")
                    else:
                        rows[identity] = _record(name, case, result)
                        clamped[name] += _clamped(name, router.calls[0][1])
    return records, clamped


def unscored(cases, reason):
    return {name: {rep: {case["id"]: _record(name, case, local=reason) for case in items}
                   for rep in REPETITIONS} for name, items in cases.items()}


def summarize(alias, cases, records, clamped, campaign):
    """Aggregate current verdicts separately from observed historical events."""
    reports = {}
    for name, items in cases.items():
        repetitions = [{"repetition": rep, **gate(name, items, records[name][rep])}
                       for rep in REPETITIONS]
        reports[name] = {"status": worst(r["status"] for r in repetitions),
                         "repetitions": repetitions}
        if name == corpus.NOVELTY:
            stored = [row.get("storage", {}) for rows in records[name].values() for row in rows.values()]
            reports[name]["storage_replay"] = {
                mode: {"replayed": sum(mode in row for row in stored),
                       "stored": sum(row.get(mode, {}).get("stored", False) for row in stored)}
                for mode in ("current_judge", "current_extractor", "promoted_judge", "promoted_extractor")
            }
    routes = {
        route: worst(reports[name]["status"] if name in reports else "incomplete" for name in names)
        for route, names in (("judge", [n for n in corpus.versions() if n != corpus.NOVELTY]),
                             ("novelty", [corpus.NOVELTY]))
    }
    lines = campaign.lines if campaign else []
    observations = [line["evidence"] if line["kind"] == "observation" else line
                    for line in lines if line["kind"] in ("observation", "answer")]
    return {
        "status": worst(routes.values(), OVERALL), "alias": alias, "routes": routes,
        "contracts": reports, "clamped_raw_scores": clamped,
        "corpus": corpus.counts(cases),
        "history": {
            "dispatched": sum(line["kind"] == "dispatch" for line in lines),
            "observations": len(observations),
            "observed_provider": sum(line.get("response_provider") is not None for line in observations),
            "unobserved_provider": sum(line.get("response_provider") is None for line in observations),
            "failures": sum(line["kind"] == "failure" for line in lines),
            "torn_tail": bool(campaign and getattr(campaign, "torn_tail", False)),
        },
        "limitations": ["Declared references are not authenticated by this report.",
                        "Synthetic embeddings do not establish retrieval quality.",
                        "This report authorizes no spending or production promotion."],
    }


async def qualify(alias, cases, params, campaign, temp_root, *, reference_policy,
                  current_reference_policy, reference_approval, source_files,
                  dispatch=False, config=None, transport=None):
    """Recompute complete admission; no caller-supplied prepared plan is authority."""
    cases, params, source_files = deepcopy(cases), deepcopy(params), deepcopy(source_files)
    config = deepcopy(config or pinned.routing())
    checked = await preflight.check(
        cases, temp_root, reference_policy=reference_policy, current_policy=current_reference_policy,
        reference_approval=reference_approval, candidates={alias: params},
        source_files=source_files, config=config,
    )
    admission = admission_identity(checked)
    if dispatch:
        _paid_eligibility(checked)
    view = None
    records = unscored(cases, "campaign_unavailable")
    clamped = dict.fromkeys(cases, 0)
    try:
        view = campaign if dispatch else ReadView(campaign)
        validate_campaign(view.state.manifest, checked, alias)
        router = pinned.PinnedRouter(view, alias=alias, params=params, dispatch=dispatch,
                                     config=config, transport=transport)
    except (ValueError, OSError):
        records = unscored(cases, "campaign_incompatible")
    else:
        records, clamped = await score(cases, router, temp_root)
    try:
        root = Path(__file__).resolve().parents[4]
        if (preflight.source_evidence(cases, source_files) != checked["reference_sources"]
            or any(preflight.file_hash(root / name) != expected
                   for name, expected in checked["production_sources"].items())
            or admission_identity(checked) != admission):
            raise Incomplete("admission_changed")
    except (ValueError, OSError):
        records, clamped = unscored(cases, "admission_changed"), dict.fromkeys(cases, 0)
    result = summarize(alias, cases, records, clamped, view)
    result["admission"] = digest(admission)
    return result, records


def pair(cases, left, right):
    """Compare identically admitted current coordinates, without selecting a winner."""
    paired = {}
    for name, items in cases.items():
        paired[name] = []
        for rep in REPETITIONS:
            row = dict.fromkeys(("cases", "valid_pairs", "matching_predictions", "both_correct",
                                 "left_only_correct", "right_only_correct", "neither_correct",
                                 "error_or_unscored_pairs"), 0)
            for case in items:
                a, b = left[name][rep][case["id"]], right[name][rep][case["id"]]
                row["cases"] += 1
                if a["error"] or a["local"] or b["error"] or b["local"]:
                    row["error_or_unscored_pairs"] += 1
                    continue
                row["valid_pairs"] += 1
                row["matching_predictions"] += a["prediction"] == b["prediction"]
                key = {(True, True): "both_correct", (True, False): "left_only_correct",
                       (False, True): "right_only_correct", (False, False): "neither_correct"}[
                           (a["agreed"], b["agreed"])]
                row[key] += 1
            paired[name].append({"repetition": rep, **row})
    return paired
