"""External review regressions using public synthetic controls and zero paid calls."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import litellm
import pytest

from genesis.eval.qualification import contracts, manifest, runner, transport
from genesis.eval.qualification.__main__ import main
from genesis.eval.qualification.evidence import Incomplete, Journal, digest
from genesis.eval.rubrics import list_rubrics
from tests.test_eval.test_qualification import call, first, priced, response, synthetic_spec


@pytest.fixture(scope="module")
async def frozen(tmp_path_factory):
    return await manifest.prepare(
        synthetic_spec(), temp_root=tmp_path_factory.mktemp("review") / "scratch"
    )


CREDENTIALS = ("API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN")


@pytest.mark.parametrize(
    ("key", "invalid"),
    [
        *[
            (key, value)
            for key in ("temperature", "top_p")
            for value in (
                False,
                True,
                None,
                "0",
                [],
                {},
                float("nan"),
                float("inf"),
                -0.01,
                10**400,
            )
        ],
        ("temperature", 2.01),
        ("top_p", 1.01),
        *[("seed", value) for value in (False, True, None, "1", 1.0, [], {})],
        *[("max_tokens", value) for value in (False, True, None, "150", 150.0, 0, -1)],
        *[("reasoning", {"max_tokens": value}) for value in (False, True, None, "1", 1.0, -1, [])],
        ("reasoning", False),
        ("reasoning", []),
    ],
)
def test_invalid_numeric_parameters_rejected_before_effective_configuration(key, invalid):
    body = synthetic_spec()["parameters"]["novelty"]
    body[key] = invalid
    with pytest.raises(Incomplete):
        manifest.effective(body, {"kwargs": {}}, None)


@pytest.mark.parametrize(
    "extra",
    [
        {"temperature": 0},
        {"temperature": 0.0},
        {"temperature": 2},
        {"temperature": 2.0},
        {"top_p": 0},
        {"top_p": 0.0},
        {"top_p": 1},
        {"top_p": 1.0},
        {"seed": 0},
        {"seed": -1},
        {"seed": 2**64},
        {"reasoning": {"max_tokens": 0}},
        {"reasoning": {"max_tokens": 2000}},
    ],
)
def test_documented_numeric_boundaries_preserved(extra):
    body = {**synthetic_spec()["parameters"]["novelty"], **extra}
    wire, _ = manifest.effective(body, {"kwargs": {}}, None)
    for key, value in extra.items():
        assert wire[key] == value
        assert type(wire[key]) is type(value)


@pytest.mark.parametrize("route", ["judge", "relevance", "novelty"])
@pytest.mark.parametrize("key", ["temperature", "top_p", "seed"])
def test_boolean_numeric_spec_cli_zero_requests_and_no_campaign(tmp_path, capsys, route, key):
    spec = synthetic_spec()
    spec["parameters"][route][key] = False
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    with patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")):
        assert (
            main(
                [
                    "prepare",
                    str(tmp_path / "campaign"),
                    "--spec",
                    str(path),
                    "--temp-root",
                    str(tmp_path / "scratch"),
                ]
            )
            == 2
        )
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
    assert not (tmp_path / "campaign").exists()


@pytest.mark.parametrize("route", ["judge", "relevance", "novelty"])
@pytest.mark.parametrize("key", ["temperature", "top_p", "seed"])
async def test_invalid_frozen_parameters_stop_execution_before_reservation(
    frozen, tmp_path, route, key
):
    data = priced(frozen)
    data["parameters"][route][key] = False
    data["pricing"]["parameters_hash"] = digest(data["parameters"])
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        with (
            patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")),
            pytest.raises(Incomplete, match="invalid numeric|invalid integer"),
        ):
            await runner.execute(journal, temp_root=tmp_path / "scratch")
        assert not journal.attempts
        assert journal.committed == 0


@pytest.mark.parametrize("invalid", [None, [], 1, "value", True])
def test_invalid_spec_root_cli_incomplete(tmp_path, capsys, invalid):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(invalid))
    with patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")):
        assert (
            main(
                [
                    "prepare",
                    str(tmp_path / "campaign"),
                    "--spec",
                    str(spec),
                    "--temp-root",
                    str(tmp_path / "scratch"),
                ]
            )
            == 2
        )
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
    assert not (tmp_path / "campaign").exists()


@pytest.mark.parametrize(
    "field", ["pricing", "parameters", "provider_policy", "existing", "target", "contract"]
)
@pytest.mark.parametrize("invalid", [None, [], 1, "value", True])
async def test_invalid_nested_spec_zero_requests(tmp_path, field, invalid):
    spec = synthetic_spec()
    if field == "provider_policy":
        spec["parameters"]["judge"]["provider"] = invalid
    elif field == "existing":
        spec["cases"][-1]["existing"] = [invalid]
    elif field == "target":
        if invalid is None:  # Null is the valid distinct label.
            return
        spec["cases"][-1]["expected_target"] = invalid
    elif field == "contract":
        spec["cases"][0]["contract"] = invalid
    else:
        spec[field] = invalid
    with patch.object(transport.LiteLLMDelegate, "call") as submit:
        with pytest.raises((Incomplete, ValueError, TypeError)):
            await manifest.prepare(spec, temp_root=tmp_path / "scratch")
        submit.assert_not_called()


@pytest.mark.parametrize("value", [10**400, 1e100, float("inf"), True, None, "1"])
async def test_unrepresentable_embeddings_rejected_before_render(tmp_path, value):
    spec = synthetic_spec()
    spec["cases"][-1]["existing"][0]["embedding"] = [value]
    with patch.object(transport.LiteLLMDelegate, "call") as submit:
        with pytest.raises(Incomplete, match="embedding"):
            await manifest.prepare(spec, temp_root=tmp_path / "scratch")
        submit.assert_not_called()


@pytest.mark.parametrize(
    "reviewer",
    ["synthetic-labeler", " synthetic-labeler ", "SYNTHETIC-LABELER", "\tSynthetic-Labeler\n"],
)
async def test_approval_identity_normalization(tmp_path, reviewer):
    spec = synthetic_spec()
    spec["cases"][0]["reference_provenance"]["reviewer"] = " synthetic-labeler "
    spec["reference_approval"] = {
        "approved": True,
        "independent": True,
        "corpus_hash": digest(spec["cases"]),
        "reviewer": reviewer,
        "evidence": "synthetic test only",
    }
    data = await manifest.prepare(spec, temp_root=tmp_path / "scratch")
    assert "independent reference approval is missing" in data["preflight_issues"]
    spec["reference_approval"]["reviewer"] = "different-person"
    assert not manifest.approval_issues(spec["cases"], spec["reference_approval"])


@pytest.mark.parametrize(
    "name",
    [
        "callbacks",
        "input_callback",
        "success_callback",
        "failure_callback",
        "_async_input_callback",
        "_async_success_callback",
        "_async_failure_callback",
        "pre_call_rules",
        "post_call_rules",
    ],
)
async def test_all_global_observer_lists_block_without_changes(frozen, tmp_path, monkeypatch, name):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    observer = object()
    with Journal(tmp_path / "campaign") as journal, patch.object(litellm, name, [observer]):
        journal.initialize(priced(frozen))
        with patch.object(transport.LiteLLMDelegate, "call") as submit:
            with pytest.raises(Incomplete, match="global"):
                await call(journal, first(frozen), lambda r: pytest.fail("HTTP"))
            submit.assert_not_called()
        assert getattr(litellm, name) == [observer]
        assert "dispatch" not in journal.attempts[first(frozen)]


@pytest.mark.parametrize("name", [r.name for r in list_rubrics()] + [contracts.RELEVANCE])
@pytest.mark.parametrize("value", [-0.01, 1.01, True, "0.9", None, 10**400])
async def test_all_judge_contracts_reject_coercion_and_clamping(name, value):
    case = next(c for c in synthetic_spec()["cases"] if c["contract"] == name)
    key = "relevance" if name == contracts.RELEVANCE else "score"
    recorder = contracts.Recorder(json.dumps({key: value}))
    try:
        result = await contracts.exercise(case, recorder)
    except Incomplete:
        pass
    else:
        assert result["error"]  # Preserve production parse-error sentinels.
    assert len(recorder.calls) == 1


@pytest.mark.parametrize(
    "content",
    ['{"score":0}', '{"score":1}', '```json\n{"score":0.8}\n```', 'Answer: {"score":0.8}'],
)
async def test_valid_rubric_numbers_and_production_json_wrappers(content):
    result = await contracts.exercise(synthetic_spec()["cases"][0], contracts.Recorder(content))
    assert result["error"] is None


def test_cli_recovers_paid_answer_without_key_before_new_reservation(
    frozen, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    key = first(data)
    campaign = tmp_path / "campaign"
    with Journal(campaign) as journal:
        journal.initialize(data)
        asyncio.run(call(journal, key, lambda r: httpx.Response(200, json=response())))
        assert "charge" in journal.attempts[key] and "score" not in journal.attempts[key]
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    with (
        patch.object(runner, "preflight", return_value=[]),
        patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")),
    ):
        assert main(["execute", str(campaign), "--temp-root", str(tmp_path / "scratch")]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["attempts"][key]["score"]["agreement"] is True
    assert result["event_counts"]["reserve"] == 1
    assert result["execution_error"]["reason"] == "an OpenRouter environment credential is required"
    with Journal(campaign) as journal:
        assert journal.attempts[key]["score"]["agreement"] is True


async def test_offline_recovery_precedes_unresolved_guard(frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    key = first(data)
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        await call(journal, key, lambda r: httpx.Response(200, json=response()))
        other = next(k for k in data["order"] if k != key)
        journal.append("reserve", attempt=other, reservation=data["schedule"][other]["maximum_usd"])
        for name in CREDENTIALS:
            monkeypatch.delenv(name, raising=False)
        with (
            patch.object(runner, "preflight", return_value=[]),
            pytest.raises(Incomplete, match="unresolved"),
        ):
            await runner.execute(journal, temp_root=tmp_path / "scratch")
        assert journal.attempts[key]["score"]["agreement"] is True
        assert "dispatch" not in journal.attempts[other]
        assert len(journal.attempts) == 2


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "libraries",
        "parameters",
        "provider_config",
        "schedule",
        "case",
        "provenance",
        "task",
        "kwargs",
        "order",
    ],
)
def test_frozen_nested_objects_fail_incomplete(frozen, field):
    data = copy.deepcopy(frozen)
    key = first(data)
    if field == "case":
        data["cases"][0] = None
    elif field == "provenance":
        data["cases"][0]["reference_provenance"] = []
    elif field == "task":
        data["schedule"][key] = None
    elif field == "kwargs":
        data["schedule"][key]["kwargs"] = []
    elif field == "order":
        data["order"][0] = []
    else:
        data[field] = None
    with pytest.raises(Incomplete):
        manifest.validate_manifest(data)


@pytest.mark.parametrize("name", [r.name for r in list_rubrics()] + [contracts.RELEVANCE])
async def test_malformed_score_keeps_charge_and_records_error(frozen, tmp_path, monkeypatch, name):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    key = first(data, name)
    field = "relevance" if name == contracts.RELEVANCE else "score"
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        await call(
            journal,
            key,
            lambda r: httpx.Response(200, json=response(content=json.dumps({field: 2}))),
        )
        async with contracts.Sandbox(tmp_path / "scratch") as sandbox:
            await runner.score_record(journal, key, sandbox)
        record = journal.attempts[key]
        assert record["charge"] == "0.01"
        assert record["score"]["error"] == "Incomplete"
        assert record["score"]["agreement"] is False
        contract = runner.report(journal)["contracts"][name]
        assert contract["repetitions"][0]["errors"] == 1


async def test_all_settled_answers_recover_without_key_or_current_prices(
    frozen, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)

    class ExpiredClock:
        fromisoformat = staticmethod(datetime.fromisoformat)

        @staticmethod
        def now(tz):
            return datetime(2100, 1, 1, tzinfo=UTC)

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        for index, key in enumerate(data["order"]):
            task = data["schedule"][key]
            if task["contract"] == contracts.NOVELTY:
                content = json.dumps(
                    {"redundant_with": task["candidate_ids"].index("candidate-b") + 1}
                )
            elif task["contract"] == contracts.RELEVANCE:
                content = '{"relevance":1}'
            else:
                content = '{"score":1}'
            await call(
                journal,
                key,
                lambda r, i=index, c=content: httpx.Response(
                    200, json=response(content=c, generation=f"gen-{i}")
                ),
            )
        for name in CREDENTIALS:
            monkeypatch.delenv(name, raising=False)
        # Test the actual price-expiry check at recovery. Bypass only synthetic
        # coverage/approval issues; source/config/version checks remain active.
        with (
            patch.object(manifest, "frozen_issues", return_value=[]),
            patch.object(manifest, "datetime", ExpiredClock),
            patch.object(manifest, "pricing_issues", wraps=manifest.pricing_issues) as prices,
            patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")),
        ):
            await runner.execute(journal, temp_root=tmp_path / "scratch")
        assert prices.call_args.kwargs["check_expiry"] is False
        assert len(journal.attempts) == 24
        assert all(a["score"]["agreement"] for a in journal.attempts.values())


@pytest.mark.parametrize("name", [r.name for r in list_rubrics()] + [contracts.RELEVANCE])
@pytest.mark.parametrize("number", ["1.0000000000000000000001", "-1e-400", "1e400"])
async def test_exact_raw_score_boundaries(name, number):
    case = next(c for c in synthetic_spec()["cases"] if c["contract"] == name)
    field = "relevance" if name == contracts.RELEVANCE else "score"
    try:
        result = await contracts.exercise(
            case, contracts.Recorder('{"' + field + '":' + number + "}")
        )
    except Incomplete:
        pass
    else:
        assert result["error"]


@pytest.mark.parametrize("name", [r.name for r in list_rubrics()] + [contracts.RELEVANCE])
async def test_initial_execution_contains_settled_parser_errors(
    frozen, tmp_path, monkeypatch, name
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    key = first(data, name)
    data["order"].remove(key)
    data["order"].insert(0, key)
    field = "relevance" if name == contracts.RELEVANCE else "score"
    dispatched = []

    class Clock:
        fromisoformat = staticmethod(datetime.fromisoformat)

        @staticmethod
        def now(tz):
            return datetime(2100 if dispatched else 2026, 1, 1, tzinfo=UTC)

    def mock(request):
        dispatched.append(request)
        return httpx.Response(200, json=response(content=json.dumps({field: 10**400})))

    with (
        Journal(tmp_path / "campaign") as journal,
        patch.object(runner, "preflight", return_value=[]),
        patch.object(manifest, "datetime", Clock),
    ):
        journal.initialize(data)
        # Expiry after the observed answer stops the next request. It must not
        # conceal the first settled malformed judgment or crash in its parser.
        with pytest.raises(Incomplete, match="expired"):
            await runner.execute(
                journal, temp_root=tmp_path / "scratch", transport=httpx.MockTransport(mock)
            )
        assert len(dispatched) == 1 and len(journal.attempts) == 1
        record = journal.attempts[key]
        assert record["charge"] == "0.01"
        assert record["score"]["error"] == "Incomplete"
        assert record["score"]["agreement"] is False
        assert runner.report(journal)["contracts"][name]["repetitions"][0]["errors"] == 1


@pytest.mark.parametrize("value", [None, [], 1, True, "value", {}])
@pytest.mark.parametrize("nested", [False, True])
async def test_malformed_reconciliation_stays_incomplete_and_never_resends(
    frozen, tmp_path, monkeypatch, value, nested
):
    from types import SimpleNamespace

    from genesis.eval.qualification import __main__ as cli

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    key = first(data)
    campaign = tmp_path / "campaign"
    with Journal(campaign) as journal:
        journal.initialize(data)
        with pytest.raises(Incomplete):
            await call(journal, key, lambda r: httpx.Response(200, json=response(cost=None)))
    seen = []

    def mock(request):
        seen.append(request)
        assert request.method == "GET"
        payload = {"data": value} if nested else value
        return httpx.Response(200, json=payload)

    original = transport.reconcile

    async def mocked_reconcile(journal):
        await original(journal, transport=httpx.MockTransport(mock))

    with patch.object(transport, "reconcile", side_effect=mocked_reconcile):
        result = await cli.run(SimpleNamespace(command="reconcile", campaign=campaign))
    assert result["status"] == "incomplete"
    assert "execution_error" in result
    assert len(seen) == 1
    assert result["event_counts"]["dispatch"] == 1  # Only the original completion.
    assert result["unresolved_attempts"] == [key]
    assert result["committed_usd"] == "0.05"
    assert "charge" not in result["attempts"][key]
    with Journal(campaign) as journal:
        assert "charge" not in journal.attempts[key]


async def test_sandbox_admission_failure_is_incomplete_before_requests(tmp_path):
    from genesis.db.integrity import DatabaseIntegrityError

    with (
        patch(
            "genesis.db.admission.assert_admitted",
            side_effect=DatabaseIntegrityError("synthetic refusal"),
        ),
        patch.object(transport.LiteLLMDelegate, "call") as submit,
    ):
        with pytest.raises(Incomplete, match="SQLite admission"):
            await manifest.prepare(synthetic_spec(), temp_root=tmp_path / "scratch")
        submit.assert_not_called()
    assert list((tmp_path / "scratch").iterdir()) == []


async def test_sandbox_case_admission_failure_keeps_template_isolated(tmp_path):
    from genesis.db.integrity import DatabaseIntegrityError

    case = synthetic_spec()["cases"][-1]
    async with contracts.Sandbox(tmp_path / "scratch") as sandbox:
        with (
            patch(
                "genesis.db.admission.assert_admitted",
                side_effect=DatabaseIntegrityError("synthetic refusal"),
            ),
            pytest.raises(Incomplete, match="SQLite admission"),
        ):
            await sandbox.database(case)
        assert sandbox.template.exists()
    assert list((tmp_path / "scratch").iterdir()) == []
