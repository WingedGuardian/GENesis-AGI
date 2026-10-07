"""Documented gateway identity, expense recovery and immutable replay eligibility."""

import copy
import json
from decimal import Decimal

import httpx
import pytest

from genesis.eval.qualification import pinned
from genesis.eval.qualification.accounting import Journal
from tests.test_eval.qualification_transport_fixtures import (
    Gateway,
    ask,
    bundle,
    router,
    routing_metadata,
)


def observation():
    raw = {
        "id": "gen-1",
        "model": "synthetic/model",
        "openrouter_metadata": routing_metadata("synthetic/model", "Synthetic Gateway Display"),
    }
    return {
        "raw_body": json.dumps(raw),
        "response_model": raw["model"],
        "response_provider": "Synthetic Gateway Display",
        "generation_id": raw["id"],
        "response_headers": {},
    }


SPEC = {
    "model": "synthetic/model",
    "receipt_provider": "Synthetic Gateway Display",
    "upstream": "Synthetic",
}


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "null",
        "array",
        "empty_candidates",
        "multiple_selected",
        "selected_bool",
        "available_null",
        "available_object",
        "candidate_null",
        "selected_missing",
        "selected_model",
        "selected_provider",
        "raw_model",
        "requested",
        "strategy",
        "attempt_zero",
        "attempt_two",
        "attempt_bool",
        "attempt_missing",
        "byok",
        "byok_missing",
        "pipeline_null",
        "pipeline_object",
        "pipeline_unknown",
        "legacy_conflict",
        "legacy_null",
        "attempts_null",
        "attempts_empty",
        "attempts_two",
        "attempts_model",
        "attempts_provider",
        "attempts_status_bool",
        "attempts_status_failure",
        "cache_hit",
        "cache_unknown",
        "generation_header_conflict",
        "generation_missing",
        "generation_empty",
        "derived_provider",
    ],
)
def test_metadata_ineligibility_cannot_be_credited(change):
    obs = observation()
    raw = json.loads(obs["raw_body"])
    meta = raw["openrouter_metadata"]
    endpoint = meta["endpoints"]["available"][0]
    if change == "missing":
        raw.pop("openrouter_metadata")
    elif change == "null":
        raw["openrouter_metadata"] = None
    elif change == "array":
        raw["openrouter_metadata"] = []
    elif change == "empty_candidates":
        meta["endpoints"]["available"] = []
    elif change == "multiple_selected":
        meta["endpoints"]["available"].append(copy.deepcopy(endpoint))
    elif change == "selected_bool":
        endpoint["selected"] = 1
    elif change == "available_null":
        meta["endpoints"]["available"] = None
    elif change == "available_object":
        meta["endpoints"]["available"] = {}
    elif change == "candidate_null":
        meta["endpoints"]["available"] = [None]
    elif change == "selected_missing":
        endpoint.pop("selected")
    elif change == "selected_model":
        endpoint["model"] = "wrong/model"
    elif change == "selected_provider":
        endpoint["provider"] = "Wrong Provider"
    elif change == "raw_model":
        raw["model"] = "wrong/model"
    elif change == "requested":
        meta["requested"] = "wrong/model"
    elif change == "strategy":
        meta["strategy"] = "fallback"
    elif change == "attempt_zero":
        meta["attempt"] = 0
    elif change == "attempt_two":
        meta["attempt"] = 2
    elif change == "attempt_bool":
        meta["attempt"] = True
    elif change == "attempt_missing":
        meta.pop("attempt")
    elif change == "byok":
        meta["is_byok"] = True
    elif change == "byok_missing":
        meta.pop("is_byok")
    elif change == "pipeline_null":
        meta["pipeline"] = None
    elif change == "pipeline_object":
        meta["pipeline"] = {}
    elif change == "pipeline_unknown":
        meta["pipeline"] = [{"type": "unknown"}]
    elif change == "legacy_conflict":
        raw["provider"] = "Wrong Provider"
    elif change == "legacy_null":
        raw["provider"] = None
    elif change.startswith("attempts_"):
        meta["attempts"] = [
            {"model": SPEC["model"], "provider": SPEC["receipt_provider"], "status": 200}
        ]
        if change == "attempts_null":
            meta["attempts"] = None
        elif change == "attempts_empty":
            meta["attempts"] = []
        elif change == "attempts_two":
            meta["attempts"] *= 2
        elif change == "attempts_model":
            meta["attempts"][0]["model"] = "wrong/model"
        elif change == "attempts_provider":
            meta["attempts"][0]["provider"] = "Wrong Provider"
        elif change == "attempts_status_bool":
            meta["attempts"][0]["status"] = True
        else:
            meta["attempts"][0]["status"] = 500
    elif change.startswith("cache_"):
        obs["response_headers"]["x-openrouter-cache-status"] = (
            "HIT" if change == "cache_hit" else "UNKNOWN"
        )
    elif change == "generation_header_conflict":
        obs["response_headers"]["x-generation-id"] = "gen-different"
    elif change == "generation_missing":
        raw.pop("id")
    elif change == "generation_empty":
        raw["id"] = ""
    elif change == "derived_provider":
        obs["response_provider"] = "Wrong Provider"
    obs["raw_body"] = json.dumps(raw)
    with pytest.raises(pinned.LocalFailure):
        pinned.verify_routing(obs, SPEC)


def test_unselected_candidates_and_additive_fields_do_not_invent_retries():
    obs = observation()
    raw = json.loads(obs["raw_body"])
    meta = raw["openrouter_metadata"]
    meta["endpoints"]["available"].append(
        {"model": "other/model", "provider": "Other", "selected": False}
    )
    meta["pipeline"] = []
    meta["future_field"] = {"opaque": True}
    meta["attempts"] = [
        {"model": SPEC["model"], "provider": SPEC["receipt_provider"], "status": 200}
    ]
    obs["raw_body"] = json.dumps(raw)
    obs["response_headers"] = {"x-generation-id": "gen-1", "x-openrouter-cache-status": "MISS"}
    pinned.verify_routing(obs, SPEC)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


async def test_actual_delegate_enforces_headers_and_accepts_documented_response(env):
    data, gateway = bundle(env), Gateway()
    with data.campaign:
        await ask(router(data, gateway), data.messages)
        evidence = data.campaign.attempt("case-1")["observations"][0]
        assert "provider" not in json.loads(evidence["raw_body"])
        assert evidence["response_provider"] == SPEC["receipt_provider"]
    assert gateway.requests[0].headers["X-OpenRouter-Metadata"] == "enabled"
    assert gateway.requests[0].headers["X-OpenRouter-Cache"] == "false"


@pytest.mark.parametrize("contradiction", [False, True])
async def test_recovery_accounts_expense_but_never_qualifies_invalid_metadata(env, contradiction):
    data, gateway = bundle(env), Gateway()

    def transport(request):
        response = gateway(request)
        if request.method != "POST":
            return response
        raw = json.loads(response.content)
        if contradiction:
            raw["provider"] = "Wrong Provider"
        else:
            raw["openrouter_metadata"]["attempt"] = 2
        return httpx.Response(200, json=raw)

    with data.campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(
                router(data, gateway, transport=httpx.MockTransport(transport)), data.messages
            )
        if contradiction:
            with pytest.raises(pinned.LocalFailure):
                await pinned.reconcile(data.campaign, "case-1", transport=gateway.transport)
            assert data.campaign.state.reserved == Decimal("0.1")
            with pytest.raises(pinned.Incomplete):
                data.campaign.acknowledge("failure:case-1", "verified synthetic expense")
        else:
            await pinned.reconcile(data.campaign, "case-1", transport=gateway.transport)
            data.campaign.acknowledge("failure:case-1", "verified synthetic expense")
            with pytest.raises(pinned.LocalFailure):
                await ask(router(data, gateway, dispatch=False), data.messages)
    assert len(gateway.requests) == 1


async def test_late_provider_contradiction_restores_maximum_and_blocks_reopen(env):
    data, gateway = bundle(env), Gateway()
    with data.campaign:
        await ask(router(data, gateway), data.messages)
        old = data.campaign.attempt("case-1")["observations"][0]
        raw = json.loads(old["raw_body"])
        raw["provider"] = "Wrong Provider"
        contrary = {**old, "raw_body": json.dumps(raw)}
        contrary.update(
            pinned._observed_identity(contrary, data.campaign.attempt("case-1")["spec"])
        )
        data.campaign.observe("case-1", contrary)
        data.campaign.fail("case-1", "retained routing contradiction")
        assert data.campaign.state.settled == 0
        assert data.campaign.state.reserved == Decimal("0.1")
    with Journal(data.campaign.directory, data.campaign.expected_manifest) as reopened:
        assert len(reopened.attempt("case-1")["observations"]) == 2
        assert reopened.state.reserved == Decimal("0.1")
        with pytest.raises(pinned.Incomplete):
            reopened.acknowledge("failure:case-1", "cannot clear identity")
        data.campaign = reopened
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, gateway, dispatch=False), data.messages)
    assert len(gateway.requests) == 1
