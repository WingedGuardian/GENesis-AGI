"""Public synthetic evidence: no credentials, private references or paid calls."""

from __future__ import annotations

import asyncio
import copy
import json
from decimal import Decimal
from unittest.mock import patch

import httpx
import litellm
import pytest

from genesis.eval.qualification import contracts, manifest, runner, transport
from genesis.eval.qualification.__main__ import main
from genesis.eval.qualification.evidence import Incomplete, Journal, digest, money
from genesis.eval.rubrics import list_rubrics


def synthetic_cases():
    cases = []
    for rubric in list_rubrics():
        cases.append(
            {
                "contract": rubric.name,
                "id": rubric.name,
                "actual": "Synthetic observation with specific evidence",
                "expected": "Synthetic expected response",
                "user_passed": True,
                "scorer_config": {
                    "rubric_name": rubric.name,
                    **{k: "Synthetic context" for k in rubric.extra_placeholders},
                },
                "reference_provenance": {
                    "label_source": "human",
                    "reviewer": "synthetic-labeler",
                    "rubric_version": rubric.version,
                },
            }
        )
    cases.append(
        {
            "contract": contracts.RELEVANCE,
            "id": "relevance",
            "query": "Synthetic query",
            "memory_content": "Synthetic memory",
            "user_passed": True,
            "reference_provenance": {
                "label_source": "human",
                "reviewer": "synthetic-labeler",
                "rubric_version": contracts.versions()[contracts.RELEVANCE],
            },
        }
    )
    cases.append(
        {
            "contract": contracts.NOVELTY,
            "id": "novelty",
            "expected_target": "candidate-b",
            "new": {
                "task_type": "new-task",
                "principle": "Synthetic new playbook",
                "steps": ["Run synthetic command"],
                "embedding": [1, 0],
            },
            "existing": [
                {
                    "id": "candidate-a",
                    "task_type": "task-a",
                    "principle": "Synthetic weaker match",
                    "steps": ["Do A"],
                    "embedding": [0.7, 0.7],
                },
                {
                    "id": "candidate-b",
                    "task_type": "task-b",
                    "principle": "Synthetic stronger match",
                    "steps": ["Do B"],
                    "embedding": [1, 0],
                },
            ],
            "reference_provenance": {
                "label_source": "human",
                "reviewer": "synthetic-labeler",
                "rubric_version": contracts.versions()[contracts.NOVELTY],
            },
        }
    )
    return cases


def synthetic_spec():
    policy = {"only": ["Synthetic"], "allow_fallbacks": False, "require_parameters": True}
    params = {
        "judge": {"max_tokens": 400, "temperature": 0.0, "provider": policy},
        "relevance": {"max_tokens": 150, "temperature": 0.0, "provider": policy},
        "novelty": {"max_tokens": 300, "provider": policy},
    }
    return {"cases": synthetic_cases(), "parameters": params}


@pytest.fixture(scope="module")
async def frozen(tmp_path_factory):
    # Deliberately lacks human approval, pricing and minimum coverage.
    return await manifest.prepare(
        synthetic_spec(), temp_root=tmp_path_factory.mktemp("frozen") / "scratch"
    )


def priced(frozen, maximum="0.05"):
    result = copy.deepcopy(frozen)
    result["pricing"] = {
        "max_input_tokens": 10000,
        "input_per_million": "0",
        "output_per_million": "0",
        "request_fee": maximum,
        "currency": "USD",
        "model_id": result["model_id"],
        "endpoint": transport.ENDPOINT,
        "parameters_hash": digest(result["parameters"]),
        "corpus_hash": result["corpus_hash"],
        "verified": True,
        "reviewer": "synthetic-pricing-reviewer",
        "evidence": "synthetic test only",
        "bound_basis": "synthetic maximum",
        "valid_until": "2099-01-01T00:00:00+00:00",
    }
    for task in result["schedule"].values():
        task["maximum_usd"] = maximum
    result["maximum_campaign_usd"] = str(Decimal(maximum) * len(result["schedule"]))
    return result


def first(frozen, contract=None):
    return next(
        k for k, t in frozen["schedule"].items() if contract is None or t["contract"] == contract
    )


def response(
    model="xiaomi/mimo-v2.6-pro",
    *,
    content='{"score": 1}',
    cost="0.01",
    usage=True,
    generation="gen-synthetic",
):
    raw = {
        "id": generation,
        "model": model,
        "object": "chat.completion",
        "created": 1,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if usage:
        raw["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        if cost is not None:
            raw["usage"]["cost"] = cost
    return raw


async def call(journal, key, mock):
    task = journal.manifest["schedule"][key]
    journal.append("reserve", attempt=key, reservation=task["maximum_usd"])
    router = transport.QualificationRouter(
        journal, key, manifest.routing(), transport=httpx.MockTransport(mock)
    )
    return await router.route_call(task["call_site"], task["messages"], **task["kwargs"])


async def test_all_contracts_render_and_freeze_offline(frozen):
    assert set(frozen["contracts"]) == {r.name for r in list_rubrics()} | {
        contracts.RELEVANCE,
        contracts.NOVELTY,
    }
    assert len(frozen["schedule"]) == 24
    novelty = frozen["schedule"][first(frozen, contracts.NOVELTY)]
    assert novelty["candidate_ids"] == ["candidate-b", "candidate-a"]
    assert frozen["preflight_issues"]
    assert frozen["maximum_campaign_usd"] is None


async def test_delegate_mocked_http_e2e_one_attempt(frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    callbacks = {
        name: list(getattr(litellm, name))
        for name in ("callbacks", "success_callback", "failure_callback", "input_callback")
    }
    seen = []

    async def mock(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        key = first(frozen)
        result = await call(journal, key, mock)
        assert result.success
        assert len(seen) == 1
        assert seen[0] == {
            "model": frozen["model_id"],
            "messages": frozen["schedule"][key]["messages"],
            **frozen["schedule"][key]["parameters"],
        }
        assert journal.attempts[key]["charge"] == "0.01"
        assert journal.committed == Decimal("0.01")
    assert callbacks == {name: list(getattr(litellm, name)) for name in callbacks}


@pytest.mark.parametrize(
    "change",
    [
        {"model": None},
        {"model": "other/model"},
        {"id": None},
        {"usage": None},
        {"usage": {"prompt_tokens": 20, "completion_tokens": 10}},
        {"usage": {"prompt_tokens": -1, "completion_tokens": 10, "cost": "0.01"}},
        {"usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": "NaN"}},
        {"usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": "0.06"}},
    ],
)
async def test_bad_provider_evidence_retains_reservation(frozen, tmp_path, monkeypatch, change):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    seen = []

    def mock(request):
        seen.append(request)
        return httpx.Response(200, json={**response(), **change})

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        with pytest.raises(Incomplete):
            await call(journal, first(frozen), mock)
        assert len(seen) == 1
        assert journal.committed == Decimal("0.05")
        assert "charge" not in journal.attempts[first(frozen)]
    with Journal(tmp_path / "campaign") as reopened:
        assert reopened.committed == Decimal("0.05")


@pytest.mark.parametrize("mode", ["throttle", "timeout", "malformed", "cancel", "observer"])
async def test_failures_are_one_attempt_and_durable(frozen, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    requests = []

    async def mock(request):
        requests.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if mode == "cancel":
            raise asyncio.CancelledError()
        if mode == "throttle":
            return httpx.Response(429, json={"error": {"message": "synthetic throttle"}})
        if mode == "observer":
            return httpx.Response(200, content="invalid response JSON")
        return httpx.Response(200, json=response(content="invalid judgment JSON"))

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        key = first(frozen)
        if mode == "malformed":
            await call(journal, key, mock)
            async with contracts.Sandbox(tmp_path / "scratch") as sandbox:
                await runner.score_record(journal, key, sandbox)
            assert journal.attempts[key]["score"]["error"] == "judge_parse_fail"
        else:
            with pytest.raises((Incomplete, asyncio.CancelledError)):
                await call(journal, key, mock)
            assert "failure" in journal.attempts[key]
        assert len(requests) == 1
    with Journal(tmp_path / "campaign") as reopened:
        assert len(reopened.attempts) == 1


async def test_raw_novelty_and_both_real_storage_paths(frozen, tmp_path):
    task = frozen["schedule"][first(frozen, contracts.NOVELTY)]
    case = frozen["cases"][task["case_index"]]
    before = dict(contracts.extractor._NOVELTY_VALIDATED_MODELS)
    async with contracts.Sandbox(tmp_path / "scratch") as sandbox:
        result = await contracts.replay_storage(
            case, task, '{"redundant_with":1}', sandbox, frozen["provider"], frozen["model_id"]
        )
    assert result["prediction"] == "candidate-b"
    assert result["storage"]["current_judge"]["stored"] is True
    assert result["storage"]["current_extractor"]["stored"] is True
    assert result["storage"]["promoted_judge"]["stored"] is False
    assert result["storage"]["promoted_extractor"]["stored"] is False
    assert before == contracts.extractor._NOVELTY_VALIDATED_MODELS


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "-0.1", {}, []])
def test_invalid_currency_never_zero(value):
    with pytest.raises(Incomplete):
        money(value)


def test_exact_budget_boundary_and_restart(frozen, tmp_path):
    data = priced(frozen, "2.5")
    keys = list(data["schedule"])
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        for key in keys[:2]:
            journal.append("reserve", attempt=key, reservation="2.5")
        assert journal.committed == Decimal(5)
        with pytest.raises(Incomplete, match="funds"):
            journal.append("reserve", attempt=keys[2], reservation="2.5")
    with Journal(tmp_path / "campaign") as journal:
        assert journal.committed == Decimal(5)
        with pytest.raises(Incomplete):
            journal.append("reserve", attempt=keys[0], reservation="2.5")


def test_competing_writers_and_conflicting_manifest(frozen, tmp_path):
    path = tmp_path / "campaign"
    with Journal(path) as journal:
        journal.initialize(frozen)
        with pytest.raises(BlockingIOError), Journal(path):
            pass
    with Journal(path) as journal:
        altered = {**frozen, "model_id": "another/model"}
        with pytest.raises(Incomplete, match="conflicting"):
            journal.initialize(altered)


@pytest.mark.parametrize("damage", ["torn", "hash", "duplicate_json"])
def test_invalid_journal_stops(frozen, tmp_path, damage):
    path = tmp_path / "campaign"
    with Journal(path) as journal:
        journal.initialize(frozen)
    file = path / "events.jsonl"
    if damage == "torn":
        with file.open("ab") as stream:
            stream.write(b'{"seq":1')
    elif damage == "hash":
        file.write_bytes(
            file.read_bytes().replace(b"genesis.qualification.v1", b"genesis.qualification.v2")
        )
    else:
        file.write_bytes(file.read_bytes().replace(b'"seq":0', b'"seq":0,"seq":0'))
    with pytest.raises(Incomplete), Journal(path):
        pass


async def test_invalid_references_zero_calls(tmp_path):
    spec = synthetic_spec()
    spec["cases"][0]["user_passed"] = "yes"
    with patch.object(transport.LiteLLMDelegate, "call") as completion, pytest.raises(ValueError):
        await manifest.prepare(spec, temp_root=tmp_path / "scratch")
    completion.assert_not_called()


async def test_incomplete_preflight_zero_calls(frozen, tmp_path):
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(frozen)
        with patch.object(transport.LiteLLMDelegate, "call") as completion:
            with pytest.raises(Incomplete):
                await runner.execute(journal, temp_root=tmp_path / "scratch")
            completion.assert_not_called()
        assert not journal.attempts
        assert runner.report(journal)["status"] == "incomplete"


async def test_reconcile_get_only_no_resend(frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    key = first(data)
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        with pytest.raises(Incomplete):
            await call(journal, key, lambda r: httpx.Response(200, json=response(cost=None)))
        seen = []

        def mock(request):
            seen.append(request.method)
            return httpx.Response(
                200,
                json={
                    "data": {"id": "gen-synthetic", "model": data["model_id"], "total_cost": "0.01"}
                },
            )

        await transport.reconcile(journal, transport=httpx.MockTransport(mock))
        assert seen == ["GET"]
        assert journal.attempts[key]["charge"] == "0.01"
        assert journal.attempts[key]["failure"]  # historical failure remains visible


def test_redaction(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-sensitive-key")
    assert "synthetic-sensitive-key" not in transport.safe_text("echo synthetic-sensitive-key")


def test_help_requires_no_credentials(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
