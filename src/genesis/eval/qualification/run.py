"""Score one alias over the corpus, apply the qualification gate, pair two aliases.

Rubrics go through ``calibration.run_calibration`` with a ``PinnedRouter``;
J9 relevance and novelty keep their own loop. ``run`` may pay for missing
answers; ``report`` is the same pipeline with dispatch off, so a report is
always a rescore of paid answers against the prompts rendered now.
"""

from __future__ import annotations

import json
from pathlib import Path

from genesis.eval.calibration import run_calibration
from genesis.eval.qualification import contracts
from genesis.eval.qualification.corpus import (
    DEFAULT_FLOOR,
    FLOORS,
    NOVELTY,
    RELEVANCE,
    counts,
    coverage,
    expected,
    label,
    versions,
)
from genesis.eval.qualification.evidence import Incomplete, digest
from genesis.eval.qualification.pinned import REPETITIONS, PinnedRouter, request_cap
from genesis.eval.rubrics import get_rubric
from genesis.eval.scorers import render_rubric_prompt

GATE = 0.8
# Within one route a failed repetition is decided (everything must pass), so fail
# outranks incomplete: reporting it incomplete would invite paying for a lost verdict.
DECIDED = ("fail", "incomplete", "pass")
# Routes and aliases are judged independently, so the top level says whether work
# remains anywhere; each route keeps its own verdict in ``routes``.
OVERALL = ("incomplete", "fail", "pass")
LIMITATIONS = [
    "References are owner-declared labels; this CLI does not authenticate them.",
    "Deterministic embeddings prove the novelty prompt path, not real retrieval quality.",
    "No production route has been promoted; the owner edits routing deliberately.",
]


def worst(statuses, order=DECIDED) -> str:
    statuses = set(statuses)
    return next((s for s in order if s in statuses), "incomplete")


def _record(name, case, result=None, local=None) -> dict:
    record = {"prediction": None, "error": None, "local": local}
    if local is None:
        record.update(result)
    record["agreed"] = (
        local is None and record["error"] is None and record["prediction"] == expected(name, case)
    )
    return record


async def score(alias, corpus: dict, params, campaign, temp_root, **options):
    """Records per contract, repetition and case id, plus the router that made them."""
    total = sum(len(cases) for cases in corpus.values())
    router = PinnedRouter(campaign, alias=alias, params=params, cap=request_cap(total), **options)
    records, clamped = {}, {}
    async with contracts.Sandbox(temp_root) as sandbox:
        snapshot = Path(sandbox.directory.name)
        for name, cases in corpus.items():
            # run_calibration re-reads its file each pass; give it the cases
            # validated at start, so an edit mid-run cannot change what is graded.
            (snapshot / f"{name}.jsonl").write_text(
                "".join(json.dumps(case) + "\n" for case in cases)
            )
            records[name], clamped[name] = {}, 0
            for repetition in REPETITIONS:
                rows, contents = await _pass(name, cases, repetition, router, snapshot, sandbox)
                records[name][repetition] = rows
                clamped[name] += sum(_clamped(name, content) for content in contents)
    return records, clamped, router


async def _pass(name, cases, repetition, router, snapshot: Path, sandbox):
    rows, contents = {}, []
    if name not in (RELEVANCE, NOVELTY):
        rubric = get_rubric(name)
        prompts = {
            digest(
                [
                    {
                        "role": "user",
                        "content": render_rubric_prompt(
                            rubric, c["actual"], c.get("expected", ""), c["scorer_config"]
                        ),
                    }
                ]
            ): c["id"]
            for c in cases
        }
        router.bind(name, repetition, prompts=prompts)
        result = await run_calibration(
            rubric=rubric,
            golden_set_path=snapshot / f"{name}.jsonl",
            router=router,
            strict_references=True,
        )
        contents = [content for _, content in router.calls]
        by_id = {c["id"]: c for c in cases}
        for outcome in result.outcomes:
            case = by_id[outcome.case_id]
            local = _local(router, case["id"])
            prediction = None if outcome.error else outcome.judge_passed
            rows[case["id"]] = _record(
                name, case, {"prediction": prediction, "error": outcome.error}, local
            )
        return rows, contents
    for case in cases:
        router.bind(name, repetition, case_id=case["id"])
        try:
            if name == RELEVANCE:
                result = await contracts.relevance(case, router)
            else:
                result = await contracts.novelty(case, router, sandbox)
        except Incomplete as exc:
            result = None
            router.local.setdefault(case["id"], str(exc))
        contents += [content for _, content in router.calls]
        rows[case["id"]] = _record(name, case, result, _local(router, case["id"]))
    return rows, contents


def _local(router, case_id):
    """Why a case is unscored, or None. Only an answer the router served is gradable."""
    if case_id in router.local:
        return router.local[case_id]
    return None if case_id in router.served else "no answer was served"


def _clamped(name, content) -> bool:
    if name == NOVELTY:
        return False
    key, rubric = ("relevance", False) if name == RELEVANCE else ("score", True)
    value = contracts.raw_score(content, key, rubric=rubric)
    return value is not None and not 0 <= value <= 1


def gate(name, cases, rows) -> dict:
    floor = FLOORS.get(name, DEFAULT_FLOOR)
    classes = {}
    for cls in (False, True):
        ids = [c["id"] for c in cases if label(name, c) is cls]
        correct = sum(rows[i]["agreed"] for i in ids)
        classes[str(cls).lower()] = {
            "denominator": len(ids),
            "correct": correct,
            "agreement": correct / len(ids) if ids else None,
        }
    unscored = sorted(i for i, r in rows.items() if r["local"])
    errors = sum(bool(r["error"]) for r in rows.values())
    correct = sum(r["agreed"] for r in rows.values())
    if unscored or any(classes[str(c).lower()]["denominator"] < n for c, n in floor.items()):
        status = "incomplete"
    elif (
        errors or any(v["agreement"] < GATE for v in classes.values()) or correct / len(rows) < GATE
    ):
        status = "fail"
    else:
        status = "pass"
    if name == NOVELTY and any(
        r["prediction"] is not None and not r["agreed"] for r in rows.values()
    ):
        # Zero tolerance: any merge that is not the labelled target destroys a
        # real procedure, so it fails even a repetition that is still incomplete.
        status = "fail"
    return {
        "status": status,
        "denominator": len(rows),
        "correct": correct,
        "errors": errors,
        "unscored": len(unscored),
        "unscored_reasons": sorted({rows[i]["local"] for i in unscored}),
        "classes": classes,
    }


def summarize(alias, corpus, records, clamped, router, campaign) -> dict:
    contracts_report = {}
    for name, cases in corpus.items():
        repetitions = [
            {"repetition": rep, **gate(name, cases, records[name][rep])} for rep in REPETITIONS
        ]
        contracts_report[name] = {
            "status": worst(r["status"] for r in repetitions),
            "repetitions": repetitions,
        }
    judge = [n for n in versions() if n != NOVELTY]
    routes = {
        route: worst(contracts_report[n]["status"] if n in corpus else "incomplete" for n in names)
        for route, names in (("judge", judge), ("novelty", [NOVELTY]))
    }
    scoped = [line for line in campaign.lines if router.in_scope(line)]
    return {
        "status": worst(routes.values(), OVERALL),
        "alias": alias,
        "model": router.cfg.model_id,
        "upstream": router.params["upstream"],
        "routes": routes,
        "contracts": contracts_report,
        "clamped_raw_scores": clamped,
        "requests": {
            "cap": router.cap,
            "dispatched": router.dispatched,
            "answered": sum(line["kind"] == "answer" for line in scoped),
            "failed": sum(line["kind"] == "failure" for line in scoped),
            # Cut off at max_tokens: an unparseable one is a model error under these params.
            "truncated": sum(line.get("finish_reason") == "length" for line in scoped),
        },
        "key": router.key_info,
        "corpus": counts(corpus),
        "coverage_issues": coverage(corpus),
        "torn_tail_discarded": campaign.torn_tail_discarded,
        "limitations": LIMITATIONS,
    }


async def qualify(alias, corpus, params, campaign, temp_root, **options) -> dict:
    records, clamped, router = await score(alias, corpus, params, campaign, temp_root, **options)
    return summarize(alias, corpus, records, clamped, router, campaign), records


def pair(corpus, left: dict, right: dict) -> dict:
    """Both sides were looked up by the prompts rendered now, so pairs share a prompt hash."""
    paired = {}
    for name in corpus:
        rows = []
        for rep in REPETITIONS:
            row = dict.fromkeys(
                (
                    "cases",
                    "valid_pairs",
                    "matching_predictions",
                    "both_correct",
                    "left_only_correct",
                    "right_only_correct",
                    "neither_correct",
                    "error_or_unscored_pairs",
                ),
                0,
            )
            for case_id, a in left[name][rep].items():
                b = right[name][rep][case_id]
                row["cases"] += 1
                if a["error"] or a["local"] or b["error"] or b["local"]:
                    row["error_or_unscored_pairs"] += 1
                    continue
                row["valid_pairs"] += 1
                row["matching_predictions"] += int(a["prediction"] == b["prediction"])
                key = {
                    (True, True): "both_correct",
                    (True, False): "left_only_correct",
                    (False, True): "right_only_correct",
                    (False, False): "neither_correct",
                }[(a["agreed"], b["agreed"])]
                row[key] += 1
            rows.append({"repetition": rep, **row})
        paired[name] = rows
    return paired
