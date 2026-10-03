"""Synthetic compile/dispatch/score boundaries; no paid inference or live DBs."""

from __future__ import annotations

import copy
import json
import sqlite3
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from genesis.eval import scorers
from genesis.eval.qualification import contracts, manifest, runner
from genesis.eval.qualification.evidence import Incomplete, Journal
from tests.test_eval.test_qualification import call, first, priced, response, synthetic_spec


@pytest.fixture(scope="module")
async def phase_frozen(tmp_path_factory):
    return await manifest.prepare(
        synthetic_spec(), temp_root=tmp_path_factory.mktemp("phase-frozen") / "scratch"
    )


@pytest.mark.parametrize("failure", ["sqlite", "storage-outcome"])
async def test_paid_local_failure_retains_unscored_answer_and_rescores_offline(
    phase_frozen, tmp_path, monkeypatch, failure
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(phase_frozen)
    key = first(data, contracts.NOVELTY)
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(200, json=response(content='{"redundant_with": null}'))

    campaign = tmp_path / "campaign"
    with Journal(campaign) as journal:
        journal.initialize(data)
        await call(journal, key, reply)
        before = (campaign / "events.jsonl").read_bytes()
        async with contracts.Sandbox(tmp_path / "scratch") as sandbox:
            injected = (
                patch.object(
                    sandbox, "database", side_effect=sqlite3.OperationalError("synthetic DB fault")
                )
                if failure == "sqlite"
                else patch.object(contracts.judge, "_store_judged_procedure", return_value=None)
            )
            with injected, pytest.raises(Incomplete):
                await runner.score_record(journal, key, sandbox)
        record = journal.attempts[key]
        assert record["charge"] == "0.01"
        assert record["observation"]["content"] == '{"redundant_with": null}'
        assert "score" not in record and "failure" not in record
        assert runner.report(journal)["status"] == "incomplete"
        assert (campaign / "events.jsonl").read_bytes() == before
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with Journal(campaign) as journal:
        async with contracts.Sandbox(tmp_path / "recovery") as sandbox:
            with patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")):
                await runner.score_record(journal, key, sandbox)
        assert journal.attempts[key]["score"]["error"] is None
        assert journal.attempts[key]["charge"] == "0.01"
    assert len(seen) == 1


@pytest.mark.parametrize("error", [OSError, TypeError, RuntimeError])
async def test_unexpected_judge_replay_failure_does_not_become_model_score(
    phase_frozen, tmp_path, monkeypatch, error
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(phase_frozen)
    key = first(data)
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        await call(journal, key, lambda r: httpx.Response(200, json=response()))
        with (
            patch.object(runner, "exercise", side_effect=error("synthetic local failure")),
            pytest.raises(Incomplete),
        ):
            await runner.score_record(journal, key, None)
        assert journal.attempts[key]["charge"] == "0.01"
        assert "score" not in journal.attempts[key]
        assert "failure" not in journal.attempts[key]


async def test_valid_saved_response_with_local_parser_failure_remains_unscored(
    phase_frozen, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(phase_frozen)
    key = first(data)
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        await call(journal, key, lambda r: httpx.Response(200, json=response()))
        with (
            patch.object(scorers, "_extract_json", return_value="local parser corruption"),
            pytest.raises(Incomplete),
        ):
            await runner.score_record(journal, key, None)
        assert "score" not in journal.attempts[key]
        assert "failure" not in journal.attempts[key]
        assert journal.attempts[key]["charge"] == "0.01"
        await runner.score_record(journal, key, None)
        assert journal.attempts[key]["score"]["agreement"] is True


async def test_paid_dispatch_uses_frozen_task_without_production_selection_or_parser(
    phase_frozen, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(phase_frozen)
    key = first(data, contracts.NOVELTY)
    seen = []

    def reply(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response(content='{"redundant_with": null}'))

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        with (
            patch.object(runner, "exercise", side_effect=AssertionError("paid production replay")),
            patch.object(runner, "score_record", new_callable=AsyncMock) as score,
        ):
            await runner.execute_attempt(
                journal, key, manifest.routing(), None, transport=httpx.MockTransport(reply)
            )
        task = data["schedule"][key]
        assert seen == [
            {"model": data["model_id"], "messages": task["messages"], **task["parameters"]}
        ]
        score.assert_awaited_once_with(journal, key, None)
        assert journal.attempts[key]["charge"] == "0.01"


@pytest.mark.parametrize(
    ("contract", "content", "expected_error"),
    [
        (None, "invalid judgment JSON", "judge_parse_fail"),
        (None, '{"score": true}', "MalformedJudgment"),
        (contracts.RELEVANCE, '{"relevance": 2}', "MalformedJudgment"),
        (contracts.NOVELTY, '{"redundant_with": true}', "MalformedJudgment"),
        (contracts.NOVELTY, '{"redundant_with": 999}', "MalformedJudgment"),
        (contracts.NOVELTY, None, "MalformedJudgment"),
    ],
)
async def test_only_malformed_saved_judgment_is_permanent_model_error(
    phase_frozen, tmp_path, monkeypatch, contract, content, expected_error
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(phase_frozen)
    key = first(data, contract)
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        await call(journal, key, lambda r: httpx.Response(200, json=response(content=content)))
        await runner.score_record(journal, key, None)
        record = journal.attempts[key]
        assert record["charge"] == "0.01"
        assert record["score"]["error"] == expected_error
        assert record["score"]["agreement"] is False
        assert "failure" not in record


@pytest.mark.parametrize("invalid_fixture", ["skip", "reusability", "impossibility"])
async def test_prepare_rejects_novelty_fixture_that_cannot_exercise_storage(
    tmp_path, invalid_fixture
):
    spec = synthetic_spec()
    case = next(c for c in spec["cases"] if c["contract"] == contracts.NOVELTY)
    if invalid_fixture == "skip":
        case["new"]["skip"] = True
    elif invalid_fixture == "reusability":
        case["new"]["reusability_score"] = 0.1
    else:
        case["new"]["principle"] = "The synthetic connector is fundamentally impossible"
    with (
        patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")),
        pytest.raises(Incomplete, match="storage"),
    ):
        await manifest.prepare(spec, temp_root=tmp_path / "scratch")


async def test_render_preflights_null_and_selected_target_for_both_storage_paths(tmp_path):
    spec = synthetic_spec()
    case = copy.deepcopy(next(c for c in spec["cases"] if c["contract"] == contracts.NOVELTY))
    controls = []
    original = contracts.replay_storage

    async def capture(*args):
        result = await original(*args)
        controls.append(result)
        return result

    async with contracts.Sandbox(tmp_path / "scratch") as sandbox:
        with patch.object(contracts, "replay_storage", side_effect=capture):
            task = await contracts.render(case, sandbox)
    assert [r["prediction"] for r in controls] == [None, task["candidate_ids"][0]]
    for result in controls:
        assert set(result["storage"]) == {
            "current_judge",
            "current_extractor",
            "promoted_judge",
            "promoted_extractor",
        }
    assert case == spec["cases"][-1]
