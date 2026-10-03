"""Exact qualification candidates, using synthetic transport and disposable storage."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import httpx
import litellm
import pytest

from genesis.eval.qualification import contracts, manifest, runner, transport
from genesis.eval.qualification.evidence import Incomplete, Journal
from tests.test_eval.test_qualification import call, first, priced, response, synthetic_spec

MIMO = "openrouter-mimo"
FLASH = "openrouter-deepseek-flash"
MODELS = {MIMO: "xiaomi/mimo-v2.6-pro", FLASH: "deepseek/deepseek-v4.1-flash"}


@pytest.fixture(scope="module", params=[MIMO, FLASH])
async def candidate(request, tmp_path_factory):
    spec = synthetic_spec()
    spec["provider"] = request.param
    return await manifest.prepare(spec, temp_root=tmp_path_factory.mktemp("candidate") / "scratch")


async def test_flash_prepare_uses_exact_shipped_candidate(tmp_path):
    spec = synthetic_spec()
    spec["provider"] = FLASH
    with patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")):
        data = await manifest.prepare(spec, temp_root=tmp_path / "scratch")
    assert data["provider"] == FLASH
    assert data["model_id"] == MODELS[FLASH]
    assert data["provider_config"]["model_id"] == MODELS[FLASH]
    assert data["endpoint"] == transport.ENDPOINT
    assert len(data["schedule"]) == 24


async def test_mimo_remains_default(tmp_path):
    data = await manifest.prepare(synthetic_spec(), temp_root=tmp_path / "scratch")
    assert data["provider"] == MIMO
    assert data["model_id"] == MODELS[MIMO]


@pytest.mark.parametrize("provider", ["openrouter-deepseek-v4", "unknown", None, [], ""])
async def test_unsupported_candidate_rejected_before_render(tmp_path, provider):
    spec = synthetic_spec()
    spec["provider"] = provider
    with (
        patch.object(manifest, "Sandbox", side_effect=AssertionError("render")),
        pytest.raises(Incomplete),
    ):
        await manifest.prepare(spec, temp_root=tmp_path / "scratch")


@pytest.mark.parametrize("provider", [MIMO, FLASH])
@pytest.mark.parametrize("field", ["model_id", "provider_type", "base_url", "name", "missing"])
async def test_candidate_configuration_rejected_before_render(tmp_path, provider, field):
    spec = synthetic_spec()
    spec["provider"] = provider
    config = copy.deepcopy(manifest.routing())
    if field == "missing":
        del config.providers[provider]
    else:
        value = {
            "model_id": "xiaomi/mimo-v2.5-pro" if provider == MIMO else "deepseek/deepseek-v4-pro",
            "provider_type": "openai",
            "base_url": "https://synthetic.invalid/api/v1",
            "name": "different-provider-alias",
        }[field]
        config.providers[provider] = replace(config.providers[provider], **{field: value})
    with (
        patch.object(manifest, "routing", return_value=config),
        patch.object(manifest, "Sandbox", side_effect=AssertionError("render")),
        pytest.raises(Incomplete),
    ):
        await manifest.prepare(spec, temp_root=tmp_path / "scratch")


async def test_selected_candidate_does_not_require_other_candidates_config(tmp_path):
    spec = synthetic_spec()
    spec["provider"] = FLASH
    config = copy.deepcopy(manifest.routing())
    del config.providers[MIMO]
    with patch.object(manifest, "routing", return_value=config):
        data = await manifest.prepare(spec, temp_root=tmp_path / "scratch")
    assert data["provider"] == FLASH


async def test_historical_mimo_v1_report_readable_but_dispatch_rejected(tmp_path):
    old_model = "xiaomi/mimo-v2.5-pro"
    data = priced(await manifest.prepare(synthetic_spec(), temp_root=tmp_path / "scratch"))
    data["model_id"] = old_model
    data["provider_config"]["model_id"] = old_model
    data["pricing"]["model_id"] = old_model
    config = copy.deepcopy(manifest.routing())
    config.providers[MIMO] = replace(config.providers[MIMO], model_id=old_model)
    campaign = tmp_path / "campaign"
    with Journal(campaign) as journal:
        journal.initialize(data)
    before = (campaign / "events.jsonl").read_bytes()
    with Journal(campaign) as journal:
        historical = runner.report(journal)
        assert historical["model_id"] == old_model
        assert historical["status"] == "incomplete"
        with (
            patch.object(manifest, "routing", return_value=config),
            patch.object(transport, "qualification_key", side_effect=AssertionError("credentials")),
            patch.object(runner, "qualification_key", side_effect=AssertionError("credentials")),
            patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")),
        ):
            with pytest.raises(Incomplete, match="candidate"):
                manifest.preflight(data)
            with pytest.raises(Incomplete, match="candidate"):
                await runner.execute(journal, temp_root=tmp_path / "execution")
    assert (campaign / "events.jsonl").read_bytes() == before


async def test_live_candidate_config_drift_rejected_before_reservation(candidate, tmp_path):
    data = priced(candidate)
    provider = data["provider"]
    config = copy.deepcopy(manifest.routing())
    config.providers[provider] = replace(config.providers[provider], provider_type="openai")
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        before = (tmp_path / "campaign" / "events.jsonl").read_bytes()
        with (
            patch.object(manifest, "routing", return_value=config),
            patch.object(runner, "qualification_key", side_effect=AssertionError("credentials")),
            patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")),
            pytest.raises(Incomplete),
        ):
            await runner.execute(journal, temp_root=tmp_path / "execution")
        assert not journal.attempts
        assert (tmp_path / "campaign" / "events.jsonl").read_bytes() == before


async def test_candidates_use_existing_exact_wire_identity_billing_and_callbacks(
    candidate, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    callbacks = {
        name: list(getattr(litellm, name))
        for name in ("callbacks", "input_callback", "success_callback", "failure_callback")
    }
    data = priced(candidate)
    key = first(data)
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json=response(model=MODELS[data["provider"]]))

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        result = await call(journal, key, reply)
        task = data["schedule"][key]
        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert str(requests[0].url) == transport.ENDPOINT + "/chat/completions"
        assert requests[0].headers["Authorization"] == "Bearer synthetic-not-a-real-key"
        assert json.loads(requests[0].content) == {
            "model": MODELS[data["provider"]],
            "messages": task["messages"],
            **task["parameters"],
        }
        assert result.provider_used == data["provider"]
        assert result.model_id == MODELS[data["provider"]]
        assert result.attempts == 1
        assert journal.attempts[key]["observation"]["generation_id"] == "gen-synthetic"
        assert journal.attempts[key]["charge"] == "0.01"
        assert journal.committed == Decimal("0.01")
    assert callbacks == {name: list(getattr(litellm, name)) for name in callbacks}


@pytest.mark.parametrize("fault", ["connect", "protocol", "timeout", "429", "model", "billing"])
async def test_candidates_never_retry_or_settle_wrong_identity_or_unknown_bill(
    candidate, tmp_path, monkeypatch, fault
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(candidate)
    key = first(data)
    requests = []

    def reply(request):
        requests.append(request)
        if fault in ("connect", "protocol", "timeout"):
            error = {
                "connect": httpx.ConnectError,
                "protocol": httpx.RemoteProtocolError,
                "timeout": httpx.ReadTimeout,
            }[fault]
            raise error("synthetic transport fault", request=request)
        if fault == "429":
            return httpx.Response(429, json={"error": {"message": "synthetic throttle"}})
        return httpx.Response(
            200,
            json=response(
                model="deepseek/deepseek-v4-pro" if fault == "model" else data["model_id"],
                cost=None if fault == "billing" else "0.01",
            ),
        )

    campaign = tmp_path / "campaign"
    with Journal(campaign) as journal:
        journal.initialize(data)
        with pytest.raises(Incomplete):
            await call(journal, key, reply)
        assert len(requests) == 1
        assert "charge" not in journal.attempts[key]
        assert journal.committed == Decimal("0.05")
    with Journal(campaign) as journal:
        assert journal.committed == Decimal("0.05")
        assert len(journal.attempts) == 1


@pytest.mark.parametrize("target", [None, 1])
async def test_candidates_actual_novelty_storage_current_and_promoted(candidate, tmp_path, target):
    data = candidate
    task = data["schedule"][first(data, contracts.NOVELTY)]
    case = data["cases"][task["case_index"]]
    original_allowlist = dict(contracts.extractor._NOVELTY_VALIDATED_MODELS)
    async with contracts.Sandbox(tmp_path / "scratch") as sandbox:
        result = await contracts.replay_storage(
            case,
            task,
            json.dumps({"redundant_with": target}),
            sandbox,
            data["provider"],
            data["model_id"],
        )
    assert result["prediction"] == (None if target is None else task["candidate_ids"][0])
    for path in ("judge", "extractor"):
        assert result["storage"][f"current_{path}"]["stored"] is True
        assert result["storage"][f"promoted_{path}"]["stored"] is (target is None)
        for arm in ("current", "promoted"):
            assert set(result["storage"][f"{arm}_{path}"]["remaining_ids"]) == set(
                task["candidate_ids"]
            )
    assert original_allowlist == contracts.extractor._NOVELTY_VALIDATED_MODELS
