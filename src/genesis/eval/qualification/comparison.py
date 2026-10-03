"""Credential-free pairing of two independently accounted qualification campaigns."""

from __future__ import annotations

from pathlib import Path

from genesis.eval.qualification.contracts import NOVELTY
from genesis.eval.qualification.evidence import Incomplete, Journal, currency_sum, digest
from genesis.eval.qualification.manifest import SUPPORTED_CANDIDATES, pricing_issues
from genesis.eval.qualification.runner import report

# Every other task field, including ordered novelty candidate IDs, is semantic.
MODEL_DEPENDENT_TASK_FIELDS = {"parameters", "delegate_parameters", "maximum_usd"}


def pair_issues(left: dict, right: dict) -> list[str]:
    issues = []
    if any(
        not isinstance(m["source"].get("files"), dict) or not m["source"]["files"]
        for m in (left, right)
    ):
        issues.append("paired campaigns require frozen source file identities")
    identities = {(m["provider"], m["model_id"]) for m in (left, right)}
    if identities != set(SUPPORTED_CANDIDATES.items()):
        issues.append("comparison requires the two distinct supported candidate identities")
    for field in (
        "format",
        "endpoint",
        "contracts",
        "corpus_hash",
        "cases",
        "reference_approval",
        "libraries",
        "order",
    ):
        if digest(left.get(field)) != digest(right.get(field)):
            issues.append(f"paired campaigns differ in {field}")
    if digest(left["source"].get("files")) != digest(right["source"].get("files")):
        issues.append("paired campaigns differ in source file identities")
    schedules = [
        {
            key: {k: v for k, v in task.items() if k not in MODEL_DEPENDENT_TASK_FIELDS}
            for key, task in m["schedule"].items()
        }
        for m in (left, right)
    ]
    if digest(schedules[0]) != digest(schedules[1]):
        issues.append("paired campaigns differ in frozen schedule semantics")
    return issues


def valid_prediction(record: dict, task: dict) -> bool:
    score = record.get("score")
    if (
        not isinstance(score, dict)
        or "charge" not in record
        or score.get("error")
        or record.get("failure")
    ):
        return False
    if "prediction" not in score or type(score.get("agreement")) is not bool:
        return False
    prediction = score["prediction"]
    if task["contract"] == NOVELTY:
        return prediction is None or (
            isinstance(prediction, str) and prediction in task["candidate_ids"]
        )
    return type(prediction) is bool


def paired_results(left, right) -> tuple[dict, bool]:
    result = {}
    invalid_evidence = False
    for contract in left.manifest["contracts"]:
        rows = []
        for repetition in (1, 2, 3):
            row = dict.fromkeys(
                (
                    "scheduled",
                    "both_scored",
                    "valid_pairs",
                    "matching_predictions",
                    "disagreements",
                    "both_correct",
                    "left_only_correct",
                    "right_only_correct",
                    "neither_correct",
                    "error_pairs",
                ),
                0,
            )
            row.update(repetition=repetition, errors={"left": 0, "right": 0})
            for key, task in left.manifest["schedule"].items():
                if task["contract"] != contract or task["repetition"] != repetition:
                    continue
                row["scheduled"] += 1
                records = [j.attempts.get(key, {}) for j in (left, right)]
                valid = [valid_prediction(r, task) for r in records]
                row["both_scored"] += int(all("score" in r for r in records))
                errors = []
                for side, record, usable in zip(("left", "right"), records, valid, strict=True):
                    score = record.get("score", {})
                    error = bool(record.get("failure") or score.get("error"))
                    # Unexpected score types are incomplete local evidence, not
                    # fabricated model agreement or a new model parse judgment.
                    if "score" in record and not usable and not error:
                        invalid_evidence = True
                        error = True
                    errors.append(error)
                    row["errors"][side] += int(error)
                row["error_pairs"] += int(any(errors))
                if not all(valid):
                    continue
                row["valid_pairs"] += 1
                scores = [r["score"] for r in records]
                matching = scores[0]["prediction"] == scores[1]["prediction"]
                row["matching_predictions"] += int(matching)
                row["disagreements"] += int(not matching)
                correct = [s["agreement"] for s in scores]
                row["both_correct"] += int(all(correct))
                row["left_only_correct"] += int(correct == [True, False])
                row["right_only_correct"] += int(correct == [False, True])
                row["neither_correct"] += int(not any(correct))
            rows.append(row)
        result[contract] = rows
    return result, invalid_evidence


def verified_maximum(manifest: dict):
    if pricing_issues(
        manifest["pricing"], manifest["parameters"], manifest["model_id"], check_expiry=False
    ):
        return None
    if manifest["pricing"].get("corpus_hash") != manifest["corpus_hash"]:
        return None
    # report() has already revalidated every task charge and their exact sum.
    return manifest["maximum_campaign_usd"]


def compare(left: Journal, right: Journal) -> dict:
    """Read locked historical evidence; never submit, settle or rewrite a request."""
    journals = (left, right)
    reports = [report(journal) for journal in journals]
    issues = pair_issues(left.manifest, right.manifest)
    paired = None
    if not issues:
        paired, invalid = paired_results(left, right)
        if invalid:
            issues.append("invalid paired prediction evidence")
    maxima = [verified_maximum(j.manifest) for j in journals]
    models = {}
    for side, journal, model_report, maximum in zip(
        ("left", "right"), journals, reports, maxima, strict=True
    ):
        provider = journal.manifest["provider"]
        key = provider if provider not in models else f"{provider}#2"
        models[key] = {
            "input": side,
            "provider": provider,
            "model_id": journal.manifest["model_id"],
            "parameters": journal.manifest["parameters"],
            "provider_config": journal.manifest["provider_config"],
            "pricing": journal.manifest["pricing"],
            "verified_maximum_usd": maximum,
            "report": model_report,
        }
    statuses = {r["status"] for r in reports}
    return {
        "format": "genesis.qualification.comparison.v1",
        "status": "incomplete"
        if issues or None in maxima or "incomplete" in statuses
        else "fail"
        if "fail" in statuses
        else "pass",
        "comparison_issues": issues,
        "left_provider": left.manifest["provider"],
        "right_provider": right.manifest["provider"],
        "models": models,
        "paired": paired,
        "eligible_models_by_route": {
            route: [
                key for key, model in models.items() if model["report"]["routes"][route] == "pass"
            ]
            for route in ("judge", "novelty")
        },
        "costs": {
            "settled_usd": str(currency_sum(r["settled_usd"] for r in reports)),
            "committed_usd": str(currency_sum(r["committed_usd"] for r in reports)),
            "unresolved_reservations_usd": str(
                currency_sum(
                    a["reservation"]
                    for j in journals
                    for a in j.attempts.values()
                    if "charge" not in a
                )
            ),
            "ceiling_usd": str(currency_sum(r["ceiling_usd"] for r in reports)),
            "maximum_campaign_usd": str(currency_sum(maxima)) if None not in maxima else None,
        },
        "limitations": [
            "Each campaign enforces its own frozen ceiling; their sum is not spending approval.",
            "Paired agreement does not establish truth; approved references grade each model independently.",
            "This report selects no winner and promotes no production route.",
        ],
    }


def compare_paths(left: Path, right: Path) -> dict:
    # Journal is create-capable. Check BOTH complete existing inputs first so a
    # typo or an empty directory cannot manufacture another campaign's files.
    for path in (left, right):
        if not path.is_dir() or not all(
            (path / name).is_file() for name in ("events.jsonl", "writer.lock")
        ):
            raise Incomplete("comparison requires two existing campaign journals")
    if left.samefile(right) or (left / "events.jsonl").samefile(right / "events.jsonl"):
        raise Incomplete("comparison inputs alias the same campaign")
    with Journal(left) as first, Journal(right) as second:
        return compare(first, second)
