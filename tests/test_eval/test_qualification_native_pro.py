"""A native Pro transport can settle only its own bound response and tariff."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from genesis.eval.qualification import (
    accounting,
    execution,
    native,
    native_expense,
    pinned,
    transports,
)
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.qualification_native_fixtures import fixture, native_environment  # noqa: F401

PRO = "deepseek-v4-pro"
PRO_KIND = "deepseek_native_pro_v1"


def pro_fixture():
    params, contract, messages, wire, binding, spec, raw = fixture()
    tariff = {
        **binding["native_tariff"], "model": PRO,
        "rates": {
            "off_peak": {"prompt_cache_hit": "0.02", "prompt_cache_miss": "0.5", "completion": "1.5"},
            "peak": {"prompt_cache_hit": "0.04", "prompt_cache_miss": "1", "completion": "3"},
        },
    }
    wire["model"] = raw["model"] = PRO
    key = "synthetic-approved-native-key"
    binding.update(model=PRO, transport_kind=PRO_KIND, native_tariff=tariff,
                   credential_fingerprint=hashlib.sha256(key.encode()).hexdigest())
    spec.update({k: v for k, v in binding.items() if k not in ("params", "endpoint")})
    spec.update(alias=PRO, request_hash=digest(wire))
    cfg = replace(native.resolve(native.ALIAS), name=PRO, model_id=PRO)
    return params, contract, messages, wire, binding, spec, raw, cfg


def test_pro_requires_explicit_transport_alias_and_tariff():
    params, contract, messages, wire, binding, spec, _, cfg = pro_fixture()
    config = SimpleNamespace(providers={PRO: cfg})
    assert transports.select(PRO, binding, config=config) is native
    assert native.request_body(PRO, params, contract, messages) == wire
    native.validate_binding(binding, cfg, params)
    native.validate_attempt(spec, binding)
    for changed in ({}, {"transport_kind": native.KIND}, {"transport_kind": None}):
        with pytest.raises(Incomplete):
            transports.select(PRO, changed)
    with pytest.raises(Incomplete):
        transports.select(native.ALIAS, binding)
    with pytest.raises(Incomplete):
        native.resolve(PRO, SimpleNamespace(providers={PRO: replace(cfg, model_id=native.MODEL)}))
    with pytest.raises(Incomplete):
        execution.selected_aliases({PRO: {}}, allow_native=True)
    for change in ({"transport_kind": native.KIND}, {"transport_kind": None}, {"alias": native.ALIAS},
                   {"native_tariff": {**spec["native_tariff"], "model": native.MODEL}}):
        with pytest.raises(Incomplete):
            native_expense.validate_spec({**spec, **change})


@pytest.mark.parametrize("response_model", [PRO, native.MODEL])
async def test_native_pro_actual_sdk_and_tariff(tmp_path, response_model):
    params, contract, messages, wire, binding, spec, raw, cfg = pro_fixture()
    raw["model"] = response_model
    config = SimpleNamespace(providers={PRO: cfg})
    journal = accounting.Journal(tmp_path / "pro", {
        "version": 2, "budget": spec["max_charge"],
        "binding": {"transports": {PRO: binding}}, "attempts": [spec],
    })
    sent = []

    def respond(request):
        assert str(request.url) == native.COMPLETION_URL
        assert json.loads(request.content) == wire
        sent.append(request)
        return httpx.Response(200, json=raw)

    async def admit(_router, _attempt, _info):
        pass

    with journal:
        router = pinned.PinnedRouter(journal, alias=PRO, params=params, dispatch=True, config=config,
                                     dispatch_admission=admit, credential_mode="shared_existing",
                                     transport=httpx.MockTransport(respond))
        router.bind(contract, 1, case_id=spec["case_id"])
        if response_model == PRO:
            assert (await router.route_call("judge", messages)).success
            assert journal.state.settled == Decimal("0.00001516")
            assert journal.state.reserved == 0
        else:
            with pytest.raises(pinned.LocalFailure):
                await router.route_call("judge", messages)
            assert journal.state.settled == 0
            assert journal.state.reserved == Decimal(spec["max_charge"])
        assert len(sent) == 1
