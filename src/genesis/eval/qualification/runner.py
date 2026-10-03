"""Execute a frozen schedule, preserve every attempt, and report route gates."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path

from genesis.eval.qualification.contracts import (
    NOVELTY,
    MalformedJudgment,
    Sandbox,
    exercise,
    replay_storage,
    validate_raw_score,
)
from genesis.eval.qualification.evidence import Incomplete, currency_sum
from genesis.eval.qualification.manifest import frozen_issues, preflight, pricing_issues, routing
from genesis.eval.qualification.transport import (
    QualificationRouter,
    check_callback_isolation,
    qualification_key,
)


async def score_record(journal, key, sandbox):
    record = journal.attempts[key]
    task = journal.manifest["schedule"][key]
    case = journal.manifest["cases"][task["case_index"]]
    from genesis.eval.qualification.contracts import Recorder

    content = record["observation"]["content"]
    try:
        if not isinstance(content, str):
            raise MalformedJudgment("missing response content")
        if task["contract"] == NOVELTY:
            result = await replay_storage(
                case,
                task,
                content,
                sandbox,
                journal.manifest["provider"],
                record["observation"]["model"],
            )
        else:
            router = Recorder(
                content, provider=journal.manifest["provider"], model=record["observation"]["model"]
            )
            result = await exercise(case, router, sandbox)
            if len(router.calls) != 1 or router.calls[0] != {
                name: task[name] for name in ("call_site", "messages", "kwargs")
            }:
                raise Incomplete("scoring replay changed the frozen request")
        if result.get("error") == "judge_parse_fail":
            validate_raw_score(content, "score", rubric=True, error=result["error"])
            raise Incomplete("production judge rejected a valid saved response")
        if result.get("error"):
            raise Incomplete("production scoring replay failed")
        expected = case["expected_target"] if task["contract"] == NOVELTY else case["user_passed"]
        result["agreement"] = result["prediction"] == expected and not result.get("error")
    except MalformedJudgment as exc:
        result = {"agreement": False, "error": exc.error, "prediction": None}
    except Exception as exc:
        # Local replay can be retried with the already-paid answer. Do not seal
        # a filesystem/database/parity failure as an immutable model judgment.
        raise Incomplete("local scoring failed; paid answer retained unscored") from exc
    journal.append("score", attempt=key, **result)


async def execute(journal, *, temp_root: Path, transport=None):
    issues = preflight(journal.manifest, check_expiry=False)
    if issues:
        raise Incomplete("; ".join(issues))
    async with Sandbox(temp_root) as sandbox:
        # Recover all locally available answers before considering another request.
        # Neither credentials nor unexpired prices are needed to replay paid answers.
        for key, record in journal.attempts.items():
            if "charge" in record and "score" not in record:
                await score_record(journal, key, sandbox)
        if any("charge" not in record for record in journal.attempts.values()):
            raise Incomplete("unresolved prior attempt; reconcile without resending")
        if all(key in journal.attempts for key in journal.manifest["order"]):
            return
        check_callback_isolation()
        qualification_key()  # Before reserving funds for any new request.
        config = routing()
        for key in journal.manifest["order"]:
            if key not in journal.attempts:
                await execute_attempt(journal, key, config, sandbox, transport=transport)


async def execute_attempt(journal, key, config, sandbox, *, transport=None):
    task = journal.manifest["schedule"][key]
    issues = pricing_issues(
        journal.manifest["pricing"],
        journal.manifest["parameters"],
        journal.manifest["model_id"],
    )
    if issues:
        raise Incomplete("; ".join(issues))
    journal.append("reserve", attempt=key, reservation=task["maximum_usd"])
    router = QualificationRouter(journal, key, config, transport=transport)
    # Rendering and storage controls were compiled before reservation. Dispatch
    # only that frozen request; production parsing/storage is offline scoring.
    await router.route_call(
        task["call_site"], deepcopy(task["messages"]), **deepcopy(task["kwargs"])
    )
    if not router.called or "charge" not in journal.attempts[key]:
        raise Incomplete("attempt did not settle; reservation retained")
    await score_record(journal, key, sandbox)


def report(journal) -> dict:
    manifest = journal.manifest
    if not manifest:
        raise Incomplete("campaign has no manifest")
    contracts = {}
    for name in manifest["contracts"]:
        reps = []
        for repetition in (1, 2, 3):
            scheduled = {
                k: t
                for k, t in manifest["schedule"].items()
                if t["contract"] == name and t["repetition"] == repetition
            }
            results = {
                k: journal.attempts[k]
                for k in scheduled
                if k in journal.attempts and "score" in journal.attempts[k]
            }
            classes = {}
            for label in (False, True):
                keys = [
                    k
                    for k, t in scheduled.items()
                    if (
                        (manifest["cases"][t["case_index"]]["expected_target"] is not None)
                        if name == NOVELTY
                        else manifest["cases"][t["case_index"]]["user_passed"]
                    )
                    is label
                ]
                correct = sum(
                    bool(results[k]["score"].get("agreement")) for k in keys if k in results
                )
                classes[str(label).lower()] = {
                    "denominator": len(keys),
                    "correct": correct,
                    "agreement": correct / len(keys) if keys else None,
                }
            errors = sum(
                bool(a["score"].get("error") or a.get("failure")) for a in results.values()
            )
            correct = sum(bool(a["score"].get("agreement")) for a in results.values())
            status = "pass"
            minima = {"false": 300, "true": 100} if name == NOVELTY else {"false": 25, "true": 25}
            if len(results) != len(scheduled) or any(
                classes[label]["denominator"] < floor for label, floor in minima.items()
            ):
                status = "incomplete"
            elif (
                errors
                or any(
                    v["agreement"] is None
                    or v["agreement"] < (1 if name == NOVELTY and label == "false" else 0.8)
                    for label, v in classes.items()
                )
                or correct / len(scheduled) < 0.8
            ):
                status = "fail"
            if name == NOVELTY and any(
                a["score"].get("prediction") is not None and not a["score"].get("agreement")
                for a in results.values()
            ):
                status = "fail"  # wrong-target suppression has zero tolerance
            reps.append(
                {
                    "repetition": repetition,
                    "status": status,
                    "denominator": len(scheduled),
                    "scored": len(results),
                    "correct": correct,
                    "errors": errors,
                    "classes": classes,
                }
            )
        statuses = {r["status"] for r in reps}
        contracts[name] = {
            "status": "incomplete"
            if "incomplete" in statuses
            else "fail"
            if "fail" in statuses
            else "pass",
            "repetitions": reps,
        }
    # Reports grade the frozen historical run. Live source and price expiry
    # checks belong to dry-run/execute, so upgrades do not erase measured evidence.
    issues = frozen_issues(manifest)
    unresolved = [k for k, a in journal.attempts.items() if "charge" not in a]
    route_status = {}
    for route, names in (("judge", [n for n in contracts if n != NOVELTY]), ("novelty", [NOVELTY])):
        statuses = {contracts[n]["status"] for n in names}
        route_status[route] = (
            "incomplete"
            if issues
            or any(manifest["schedule"][k]["contract"] in names for k in unresolved)
            or "incomplete" in statuses
            else "fail"
            if "fail" in statuses
            else "pass"
        )
    charge = currency_sum(a["charge"] for a in journal.attempts.values() if "charge" in a)
    return {
        "status": "incomplete"
        if "incomplete" in route_status.values()
        else "fail"
        if "fail" in route_status.values()
        else "pass",
        "routes": route_status,
        "contracts": contracts,
        "preflight_issues": issues,
        "requests_scheduled": len(manifest["schedule"]),
        "attempts": journal.attempts,
        "events": len(journal.events),
        "event_counts": dict(Counter(e["kind"] for e in journal.events)),
        "settled_usd": str(charge),
        "unresolved_attempts": unresolved,
        "committed_usd": str(journal.committed),
        "ceiling_usd": manifest["ceiling_usd"],
        "maximum_campaign_usd": manifest["maximum_campaign_usd"],
        "model_id": manifest["model_id"],
        "source": manifest["source"],
        "limitations": [
            "Declared human approval is not authenticated by this CLI.",
            "Deterministic embeddings and extraction stubs prove storage integration only.",
            "No production route has been promoted.",
        ],
    }
