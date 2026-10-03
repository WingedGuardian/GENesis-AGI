"""Synthetic paired evidence; neither model supplies reference truth."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from genesis.eval.qualification import contracts, manifest, runner, transport
from genesis.eval.qualification.__main__ import main
from genesis.eval.qualification.evidence import Journal, currency_sum, digest
from tests.test_eval.test_qualification import first, priced, response, synthetic_spec
from tests.test_eval.test_qualification_gates import full as full

FLASH = "openrouter-deepseek-flash"
FLASH_MODEL = "deepseek/deepseek-v4.1-flash"


@pytest.fixture(scope="module")
async def pair(tmp_path_factory):
    spec = synthetic_spec()
    root = tmp_path_factory.mktemp("paired")
    mimo = priced(await manifest.prepare(spec, temp_root=root / "mimo"))
    spec["provider"] = FLASH
    flash = priced(await manifest.prepare(spec, temp_root=root / "flash"), "0.025")
    return mimo, flash


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
            "reservation": task["maximum_usd"],
            "charge": "0.001",
            "score": {"agreement": True, "prediction": prediction, "error": None},
        }
    return SimpleNamespace(
        manifest=copy.deepcopy(data),
        attempts=attempts,
        events=[],
        committed=currency_sum(a["charge"] for a in attempts.values()),
    )


def compare(left, right):
    from genesis.eval.qualification.comparison import compare as paired_report

    return paired_report(left, right)


def test_pair_retains_three_repetitions_and_independent_reports(pair):
    result = compare(*(completed(d) for d in pair))
    assert result["status"] == "incomplete"  # Eight controls cannot qualify either model.
    assert result["comparison_issues"] == []
    assert set(result["models"]) == {"openrouter-mimo", FLASH}
    assert result["costs"]["settled_usd"] == "0.048"
    assert Decimal(result["costs"]["maximum_campaign_usd"]) == Decimal("1.8")
    assert result["costs"]["ceiling_usd"] == "10"
    for name in pair[0]["contracts"]:
        rows = result["paired"][name]
        assert [row["repetition"] for row in rows] == [1, 2, 3]
        assert all(
            row["scheduled"] == row["valid_pairs"] == row["matching_predictions"] == 1
            for row in rows
        )
        assert all(row["both_correct"] == 1 and row["disagreements"] == 0 for row in rows)


@pytest.mark.parametrize(
    "field",
    [
        "cases",
        "reference_approval",
        "contracts",
        "source",
        "libraries",
        "messages",
        "kwargs",
        "call_site",
        "candidate_ids",
        "unknown_task_field",
        "order",
    ],
)
def test_every_input_and_mapping_confound_is_incomplete(pair, field):
    left, right = (completed(d) for d in pair)
    data = right.manifest
    task = data["schedule"][first(data, contracts.NOVELTY)]
    if field == "cases":
        data["cases"][0]["user_passed"] = False
        data["corpus_hash"] = digest(data["cases"])
        data["pricing"]["corpus_hash"] = data["corpus_hash"]
    elif field == "source":
        data["source"]["files"]["config/model_routing.yaml"] = "changed"
    elif field == "contracts":
        name = next(iter(data[field]))
        data[field][name] = "changed"
        for case in data["cases"]:
            if case["contract"] == name:
                case["reference_provenance"]["rubric_version"] = "changed"
        data["corpus_hash"] = digest(data["cases"])
        data["pricing"]["corpus_hash"] = data["corpus_hash"]
    elif field in ("reference_approval", "libraries"):
        data[field][next(iter(data[field]), "evidence")] = "changed"
    elif field == "messages":
        # The in-memory prepare result shares rendered messages across repeats;
        # on-disk JSON reloads do not. Keep this a valid single-task confound.
        task["messages"] = copy.deepcopy(task["messages"])
        task["messages"][0]["content"] += " changed"
        task["prompt_hash"] = digest(task["messages"])
    elif field == "kwargs":
        task["kwargs"]["chain_offset"] = 0
    elif field == "candidate_ids":
        task[field].reverse()
    elif field == "order":
        data["order"].reverse()
    else:
        task[field] = "changed"
    result = compare(left, right)
    assert result["status"] == "incomplete"
    assert result["comparison_issues"]
    assert result["paired"] is None
    assert len(result["models"][FLASH]["report"]["attempts"]) == 24


def test_same_candidate_is_not_a_comparison(pair):
    result = compare(completed(pair[0]), completed(pair[0]))
    assert result["status"] == "incomplete"
    assert result["comparison_issues"]
    assert result["paired"] is None


@pytest.mark.parametrize("outcome", ["pass", "fail", "incomplete"])
def test_complete_gates_keep_route_eligibility_independent(full, outcome):
    flash = copy.deepcopy(full)
    flash.update(provider=FLASH, model_id=FLASH_MODEL)
    flash["provider_config"] = asdict(manifest.routing().providers[FLASH])
    flash["pricing"]["model_id"] = FLASH_MODEL
    left, right = completed(full), completed(flash)
    if outcome != "pass":
        key = first(flash, contracts.NOVELTY)
        if outcome == "fail":
            right.attempts[key]["score"].update(prediction="candidate-a", agreement=False)
        else:
            right.attempts[key].pop("score")
    result = compare(left, right)
    assert result["status"] == outcome
    assert result["eligible_models_by_route"]["judge"] == ["openrouter-mimo", FLASH]
    assert result["eligible_models_by_route"]["novelty"] == (
        ["openrouter-mimo", FLASH] if outcome == "pass" else ["openrouter-mimo"]
    )
    assert len(result["models"][FLASH]["report"]["attempts"]) == 2250


def test_equal_missing_source_identity_does_not_prove_pairing(pair):
    left, right = (completed(d) for d in pair)
    for journal in (left, right):
        journal.manifest["source"].pop("files")
    result = compare(left, right)
    assert result["status"] == "incomplete"
    assert result["comparison_issues"]
    assert result["paired"] is None


def test_source_commits_can_differ_with_identical_frozen_files(pair):
    left, right = (completed(d) for d in pair)
    right.manifest["source"]["commit"] = "changed-docs-only-commit"
    result = compare(left, right)
    assert result["comparison_issues"] == []
    assert result["paired"] is not None


async def test_both_campaigns_mocked_http_scoring_comparison_and_restart(
    pair, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    paths = [tmp_path / "mimo", tmp_path / "flash"]
    for data, path in zip(pair, paths, strict=True):
        requests = []

        def reply(request, *, data=data, requests=requests):
            task = data["schedule"][data["order"][len(requests)]]
            assert json.loads(request.content) == {
                "model": data["model_id"],
                "messages": task["messages"],
                **task["parameters"],
            }
            requests.append(request)
            content = (
                '{"redundant_with":1}'
                if task["contract"] == contracts.NOVELTY
                else '{"relevance":1}'
                if task["contract"] == contracts.RELEVANCE
                else '{"score":1}'
            )
            return httpx.Response(
                200,
                json=response(
                    model=data["model_id"],
                    content=content,
                    cost="0.001",
                    generation=f"gen-synthetic-{len(requests)}",
                ),
            )

        # These tiny public controls cannot qualify either candidate. Full gate
        # controls separately validate all 750 references without this bypass.
        with Journal(path) as journal, patch.object(runner, "preflight", return_value=[]):
            journal.initialize(data)
            await runner.execute(
                journal, temp_root=tmp_path / "scratch", transport=httpx.MockTransport(reply)
            )
            assert len(requests) == 24
            assert all(a["score"]["agreement"] for a in journal.attempts.values())
    with Journal(paths[0]) as left, Journal(paths[1]) as right:
        result = compare(left, right)
        assert result["comparison_issues"] == []
        assert result["costs"]["settled_usd"] == "0.048"
        assert all(
            row["valid_pairs"] == row["matching_predictions"] == 1
            for rows in result["paired"].values()
            for row in rows
        )
    monkeypatch.delenv("OPENROUTER_API_KEY")
    for path in paths:
        with Journal(path) as journal, patch.object(runner, "preflight", return_value=[]):
            await runner.execute(
                journal,
                temp_root=tmp_path / "scratch",
                transport=httpx.MockTransport(lambda r: pytest.fail("resend")),
            )
            assert len(journal.attempts) == 24


@pytest.mark.parametrize("kind", ["malformed", "failure", "unscored", "invalid_boolean"])
def test_errors_and_missing_scores_never_match_as_predictions(pair, kind):
    left, right = (completed(d) for d in pair)
    key = first(left.manifest)
    for journal in (left, right):
        record = journal.attempts[key]
        if kind == "malformed":
            record["score"].update(prediction=None, error="parse_fail", agreement=False)
        elif kind == "failure":
            record["failure"] = {"error": "observer_failure"}
        elif kind == "unscored":
            record.pop("score")
        else:
            record["score"]["prediction"] = 1
    result = compare(left, right)
    row = result["paired"][left.manifest["schedule"][key]["contract"]][0]
    assert row["valid_pairs"] == row["matching_predictions"] == row["both_correct"] == 0


def test_valid_false_and_distinct_novelty_predictions_count(pair):
    left, right = (completed(d) for d in pair)
    for journal in (left, right):
        for key, task in journal.manifest["schedule"].items():
            journal.attempts[key]["score"].update(
                prediction=None if task["contract"] == contracts.NOVELTY else False,
                agreement=False,
            )
    result = compare(left, right)
    assert all(
        row["valid_pairs"] == row["matching_predictions"] == 1
        for rows in result["paired"].values()
        for row in rows
    )


def test_unknown_or_unverified_maximum_stays_unknown(pair):
    left, right = (completed(d) for d in pair)
    right.manifest["pricing"]["verified"] = False
    result = compare(left, right)
    assert result["costs"]["maximum_campaign_usd"] is None
    assert result["status"] == "incomplete"
    assert result["models"][FLASH]["verified_maximum_usd"] is None


def test_exact_liability_sum_with_unresolved_restart(pair, tmp_path):
    paths = [tmp_path / "mimo", tmp_path / "flash"]
    reservations = ["0.050000000000000000000000000001", "0.025000000000000000000000000002"]
    for data, path, reserve in zip(pair, paths, reservations, strict=True):
        with Journal(path) as journal:
            exact = priced(data, reserve)
            exact["maximum_campaign_usd"] = str(
                currency_sum(t["maximum_usd"] for t in exact["schedule"].values())
            )
            journal.initialize(exact)
            journal.append("reserve", attempt=first(data), reservation=reserve)
    with Journal(paths[0]) as left, Journal(paths[1]) as right:
        result = compare(left, right)
        assert result["costs"]["settled_usd"] == "0"
        assert result["costs"]["committed_usd"] == "0.075000000000000000000000000003"
        assert result["costs"]["unresolved_reservations_usd"] == result["costs"]["committed_usd"]
        assert len(result["models"][FLASH]["report"]["unresolved_attempts"]) == 1


def test_cli_rejects_missing_or_aliased_inputs_without_creating_journals(pair, tmp_path, capsys):
    left, right = tmp_path / "mimo", tmp_path / "flash"
    with Journal(left) as journal:
        journal.initialize(pair[0])
    before = (left / "events.jsonl").read_bytes()
    assert main(["compare", str(left), str(right)]) == 2
    assert not right.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
    right.mkdir(mode=0o700)
    assert main(["compare", str(left), str(right)]) == 2
    assert list(right.iterdir()) == []
    capsys.readouterr()
    for alias in (left, left / ".." / "mimo"):
        assert main(["compare", str(left), str(alias)]) == 2
        capsys.readouterr()
    link = tmp_path / "alias"
    link.symlink_to(left, target_is_directory=True)
    assert main(["compare", str(left), str(link)]) == 2
    capsys.readouterr()
    assert (left / "events.jsonl").read_bytes() == before


def test_cli_holds_both_locks_and_retains_evidence(pair, tmp_path, monkeypatch, capsys):
    from genesis.eval.qualification import comparison

    paths = [tmp_path / "mimo", tmp_path / "flash"]
    for data, path in zip(pair, paths, strict=True):
        with Journal(path) as journal:
            journal.initialize(data)
    before = [(p / "events.jsonl").read_bytes() for p in paths]
    actual = comparison.compare

    def locked(left, right):
        for path in paths:
            with pytest.raises(BlockingIOError), Journal(path):
                pass
        return actual(left, right)

    monkeypatch.setattr(comparison, "compare", locked)
    with (
        patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")),
        patch.object(transport, "qualification_key", side_effect=AssertionError("credentials")),
    ):
        assert main(["compare", *map(str, paths)]) == 2
    assert json.loads(capsys.readouterr().out)["comparison_issues"] == []
    assert [(p / "events.jsonl").read_bytes() for p in paths] == before
    with Journal(paths[1]):
        assert main(["compare", *map(str, paths)]) == 2
    capsys.readouterr()
    with Journal(paths[0]):
        pass  # First lock released when the second was unavailable.


def test_compare_fresh_process_is_credential_free_and_zero_http(pair, tmp_path):
    paths = [tmp_path / "mimo", tmp_path / "flash"]
    for data, path in zip(pair, paths, strict=True):
        with Journal(path) as journal:
            journal.initialize(data)
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "LITELLM_LOCAL_MODEL_COST_MAP",
            "API_KEY_OPENROUTER",
            "OPENROUTER_API_KEY",
            "OPENROUTER_API_TOKEN",
        )
    }
    env["PYTHONPATH"] = str(manifest.ROOT / "src")
    script = """
import httpx, os, sys
def forbidden(*a, **kw): raise AssertionError("offline HTTP")
httpx.get = forbidden
httpx.Client.send = forbidden
httpx.AsyncClient.send = forbidden
from genesis.eval.qualification.__main__ import main
assert "litellm" not in sys.modules
assert main(sys.argv[1:]) == 2
assert "LITELLM_LOCAL_MODEL_COST_MAP" not in os.environ
"""
    result = subprocess.run(
        [sys.executable, "-c", script, "compare", *map(str, paths)],
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["comparison_issues"] == []
