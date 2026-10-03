"""Synthetic full-size gate controls and interruption boundaries; no network."""

from __future__ import annotations

import copy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from genesis.eval.qualification import contracts, manifest, runner, transport
from genesis.eval.qualification.evidence import Incomplete, Journal, digest
from tests.test_eval.test_qualification import call, first, priced, synthetic_spec


@pytest.fixture(scope="module")
async def full(tmp_path_factory):
    spec = synthetic_spec()
    cases = []
    for original in spec["cases"]:
        name = original["contract"]
        for index in range(400 if name == contracts.NOVELTY else 50):
            case = copy.deepcopy(original)
            case["id"] += f"-{index}"
            if name == contracts.NOVELTY:
                case["new"]["task_type"] += f"-{index}"
                case["new"]["principle"] += f" ({index})"
                case["expected_target"] = None if index < 300 else "candidate-b"
            elif name == contracts.RELEVANCE:
                case["query"] += f" ({index})"
                case["user_passed"] = index < 25
            else:
                case["actual"] += f" ({index})"
                case["user_passed"] = index < 25
            cases.append(case)
    spec["cases"] = cases
    # All approval identities here are synthetic test controls, never assertions
    # about a human's action. No completions are sent by this fixture.
    spec["reference_approval"] = {
        "approved": True,
        "independent": True,
        "corpus_hash": digest(cases),
        "reviewer": "synthetic-independent-reviewer",
        "evidence": "unit-test control",
    }
    spec["pricing"] = {
        "input_per_million": "0",
        "output_per_million": "0",
        "request_fee": "0.001",
        "max_input_tokens": 10000,
        "currency": "USD",
        "model_id": "xiaomi/mimo-v2.6-pro",
        "endpoint": transport.ENDPOINT,
        "parameters_hash": digest(spec["parameters"]),
        "corpus_hash": digest(cases),
        "verified": True,
        "reviewer": "synthetic-pricing-reviewer",
        "evidence": "unit-test control",
        "bound_basis": "mock transport only",
        "valid_until": "2099-01-01T00:00:00+00:00",
    }
    return await manifest.prepare(spec, temp_root=tmp_path_factory.mktemp("full") / "scratch")


def completed(data):
    attempts = {}
    for key, task in data["schedule"].items():
        case = data["cases"][task["case_index"]]
        prediction = (
            case["expected_target"]
            if task["contract"] == contracts.NOVELTY
            else case["user_passed"]
        )
        attempts[key] = {
            "reservation": "0.001",
            "charge": "0.001",
            "score": {"agreement": True, "prediction": prediction, "error": None},
        }
    return SimpleNamespace(manifest=data, attempts=attempts, events=[], committed=Decimal("2.25"))


async def test_full_protocol_bounds_and_oracle(full):
    assert not full["preflight_issues"]
    assert not manifest.frozen_issues(full)
    assert len(full["cases"]) == 750
    assert len(full["schedule"]) == 2250
    assert Decimal(full["maximum_campaign_usd"]) == Decimal("2.250")
    result = runner.report(completed(full))
    assert result["routes"] == {"judge": "pass", "novelty": "pass"}
    assert all(r["errors"] == 0 for c in result["contracts"].values() for r in c["repetitions"])


@pytest.mark.parametrize(
    "scenario",
    [
        "misses20",
        "misses21",
        "wrong_target",
        "false_suppression",
        "judge_class",
        "error",
        "unresolved",
    ],
)
async def test_gate_boundaries_and_independent_routes(full, scenario):
    journal = completed(copy.deepcopy(full))
    novelty = [
        k
        for k, t in full["schedule"].items()
        if t["contract"] == contracts.NOVELTY and t["repetition"] == 1
    ]
    redundant = [k for k in novelty if journal.attempts[k]["score"]["prediction"] is not None]
    if scenario.startswith("misses"):
        for key in redundant[: int(scenario.removeprefix("misses"))]:
            journal.attempts[key]["score"].update(prediction=None, agreement=False)
    elif scenario in ("wrong_target", "false_suppression"):
        key = redundant[0] if scenario == "wrong_target" else novelty[0]
        journal.attempts[key]["score"].update(prediction="candidate-a", agreement=False)
    elif scenario == "judge_class":
        keys = [
            k
            for k, t in full["schedule"].items()
            if t["contract"] == "bench_task_success"
            and t["repetition"] == 1
            and journal.attempts[k]["score"]["prediction"] is True
        ]
        for key in keys[:6]:
            journal.attempts[key]["score"].update(prediction=False, agreement=False)
    elif scenario == "error":
        journal.attempts[novelty[0]]["score"].update(
            error="synthetic parse failure", agreement=False
        )
    else:
        journal.attempts[novelty[0]].pop("charge")
        journal.attempts[novelty[0]].pop("score")
    result = runner.report(journal)
    if scenario == "judge_class":
        assert result["routes"] == {"judge": "fail", "novelty": "pass"}
    else:
        assert result["routes"]["judge"] == "pass"
        assert result["routes"]["novelty"] == (
            "pass"
            if scenario == "misses20"
            else "incomplete"
            if scenario == "unresolved"
            else "fail"
        )


async def test_preflight_recomputes_prerequisites_not_cached_status(full):
    data = copy.deepcopy(full)
    data["reference_approval"]["approved"] = False
    data["preflight_issues"] = []
    assert "independent reference approval is missing" in manifest.preflight(data)
    data = copy.deepcopy(full)
    data["pricing"]["request_fee"] = "0.01"
    for task in data["schedule"].values():
        task["maximum_usd"] = "0.01"
    data["maximum_campaign_usd"] = "22.50"
    assert any("exceeds $5 by $17.50" in issue for issue in manifest.preflight(data))


@pytest.mark.parametrize("point", ["reserve", "dispatch", "observation", "settle"])
async def test_restart_never_resends_ambiguous_attempt(full, tmp_path, point, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = copy.deepcopy(full)
    key = first(data)
    task = data["schedule"][key]
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        journal.append("reserve", attempt=key, reservation=task["maximum_usd"])
        if point != "reserve":
            journal.append("dispatch", attempt=key)
        if point in ("observation", "settle"):
            journal.append(
                "observation",
                attempt=key,
                request_hash=digest(
                    {"model": data["model_id"], "messages": task["messages"], **task["parameters"]}
                ),
                request_url=data["endpoint"] + "/chat/completions",
                model=data["model_id"],
                generation_id="synthetic-generation",
                usage={"prompt_tokens": 20, "completion_tokens": 10, "cost": "0.0001"},
                content='{"score":1}',
                http_status=200,
            )
        if point == "settle":
            journal.append(
                "settle",
                attempt=key,
                billing={
                    "source": "response.usage.cost",
                    "id": "synthetic-generation",
                    "model": data["model_id"],
                    "total_cost": "0.0001",
                },
            )
    with (
        Journal(tmp_path / "campaign") as journal,
        patch.object(transport.LiteLLMDelegate, "call") as submit,
    ):
        if point == "settle":
            # Recover scoring first, then interrupt before the NEXT dispatch.
            async with contracts.Sandbox(tmp_path / "scratch") as sandbox:
                await runner.score_record(journal, key, sandbox)
            assert "score" in journal.attempts[key]
        else:
            with pytest.raises(Incomplete, match="unresolved"):
                await runner.execute(journal, temp_root=tmp_path / "scratch")
        submit.assert_not_called()


@pytest.mark.parametrize("number", [True, 0, -1, 11, "1", 1.5])
def test_invalid_novelty_target(number):
    with pytest.raises(Incomplete):
        contracts.raw_target(__import__("json").dumps({"redundant_with": number}), ["candidate"])


async def test_serialized_request_mismatch_blocks_before_http(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(await manifest.prepare(synthetic_spec(), temp_root=tmp_path / "scratch"))
    key = first(data)
    data["schedule"][key]["delegate_parameters"]["extra_body"]["models"] = ["unapproved/fallback"]
    seen = []
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        with pytest.raises(Incomplete):
            await call(journal, key, lambda r: seen.append(r))
        assert seen == []


@pytest.mark.parametrize(
    "content", ['{"relevance":NaN}', '{"relevance":true}', '{"relevance":null}']
)
async def test_j9_invalid_raw_scores_are_qualification_errors(content):
    case = next(c for c in synthetic_spec()["cases"] if c["contract"] == contracts.RELEVANCE)
    recorder = contracts.Recorder(content)
    with pytest.raises(Incomplete):
        await contracts.exercise(case, recorder)
