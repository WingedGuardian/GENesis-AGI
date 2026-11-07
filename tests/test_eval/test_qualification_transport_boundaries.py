"""Regression controls for metadata, diagnostics, funding and local setup."""

import logging
from decimal import Decimal, localcontext

import httpx
import pytest

from genesis.eval.qualification import pinned
from tests.test_eval.qualification_transport_fixtures import Gateway, ask, bundle, router


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    path = tmp_path / "secrets.env"
    path.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(path))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.parametrize("area", ["key", "effective_budget", "budgets"])
@pytest.mark.parametrize("required,accepted", [("0.1", True), ("0.10001", False), ("0.5", False)])
async def test_snapshot_cannot_fund_more_than_either_reported_or_derived_credit(area, required, accepted):
    data = {"limit": 1, "limit_remaining": 1, "usage": 0, "limit_reset": None}
    budget = {"limit_usd": 1, "spend_usd": 0.9, "remaining_usd": 0.9}
    if area == "key":
        data.update(limit_remaining=0.9, usage=0.9)
    else:
        data[area] = budget if area == "effective_budget" else [budget]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data}))
    if accepted:
        info = await pinned.key_status("synthetic-only-key", transport, required=required)
        assert Decimal(info["available"]) == Decimal("0.1")
        if area != "key":
            assert info["budget_headroom"] == [
                {"limit_usd": "1", "spend_usd": "0.9", "available": "0.1"}
            ]
    else:
        with pytest.raises(pinned.LocalFailure, match="credit"):
            await pinned.key_status("synthetic-only-key", transport, required=required)


@pytest.mark.parametrize("area", ["key", "effective_budget", "budgets"])
async def test_credit_subtraction_preserves_lexical_precision_under_small_context(area):
    payload = '{"limit":1,"usage":0,"limit_remaining":1,"limit_reset":null}'
    if area == "key":
        payload = payload.replace('"usage":0', '"usage":0.99999999999999999999999999999')
    else:
        budget = '{"limit_usd":1,"spend_usd":0.99999999999999999999999999999,"remaining_usd":1}'
        payload = payload[:-1] + ',"' + area + '":' + (budget if area == "effective_budget" else '[' + budget + ']') + '}'
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content='{"data":' + payload + '}'))
    with localcontext() as context:
        context.prec = 2
        info = await pinned.key_status("synthetic-only-key", transport, required="0.00000000000000000000000000001")
    assert Decimal(info["available"]) == Decimal("0.00000000000000000000000000001")


@pytest.mark.parametrize("field", ["limit_usd", "spend_usd"])
@pytest.mark.parametrize("value", [None, True, "0.9", -1, "missing"])
async def test_supplied_budget_constraints_are_not_silently_ignored(field, value):
    budget = {"limit_usd": 1, "spend_usd": 0.9, "remaining_usd": 0.9}
    if value == "missing":
        budget.pop(field)
    else:
        budget[field] = value
    data = {"limit": 1, "limit_remaining": 1, "usage": 0, "limit_reset": None, "effective_budget": budget}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data}))
    with pytest.raises(pinned.LocalFailure):
        await pinned.key_status("synthetic-only-key", transport, required="0.1")


@pytest.mark.parametrize("invalid", ["cyclic", "object", "nan", "mixed_keys"])
async def test_invalid_association_is_refused_before_recovery_get(env, invalid):
    data, gateway = bundle(env), Gateway()
    extra = {"cyclic": [], "object": object(), "nan": float("nan"), "mixed_keys": {1: "a", "b": "c"}}[invalid]
    if invalid == "cyclic":
        extra.append(extra)
    with data.campaign:
        data.campaign.reserve("case-1")
        data.campaign.dispatch("case-1")
        spec = data.campaign.attempt("case-1")["spec"]
        before = list(data.campaign.lines)
        with pytest.raises(pinned.LocalFailure, match="association"):
            await pinned.reconcile(data.campaign, "case-1", transport=gateway.transport, association={
                "attempt": "case-1", "request_hash": spec["request_hash"],
                "generation_id": "synthetic-gen", "evidence": "synthetic operator record", "extra": extra,
            })
        assert data.campaign.lines == before
    assert not gateway.requests and gateway.key_reads == gateway.receipt_reads == 0


@pytest.mark.parametrize("area", ["message", "args", "exception", "stack", "cached_exception", "redaction"])
def test_all_log_rendering_is_total_and_restores_the_factory(env, monkeypatch, area):
    class Unprintable:
        def __str__(self):
            raise KeyboardInterrupt("synthetic-only-key")

    original = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = original(*args, **kwargs)
        if area == "stack":
            record.stack_info = Unprintable()
        if area == "cached_exception":
            record.exc_text = "synthetic-only-key"
        return record

    monkeypatch.setattr(logging, "_logRecordFactory", factory)
    if area == "redaction":
        monkeypatch.setattr(pinned, "safe_text", lambda _: (_ for _ in ()).throw(ValueError("synthetic-only-key")))
    if area == "exception":
        monkeypatch.setattr(pinned.traceback, "format_exception", lambda *_: (_ for _ in ()).throw(ValueError("synthetic-only-key")))
    with pinned.private_logs():
        record = logging.getLogger("boundary-probe").makeRecord(
            "boundary-probe", logging.WARNING, __file__, 1,
            Unprintable() if area == "message" else "%s %s" if area == "args" else "synthetic-only-key",
            ("one",) if area == "args" else (),
            (ValueError, ValueError("synthetic-only-key"), None) if area == "exception" else None,
        )
        rendered = logging.Formatter().format(record)
        assert "synthetic-only-key" not in rendered and "[REDACTED" in rendered
        assert record.args == () and record.exc_info is None
    assert logging.getLogRecordFactory() is factory


async def test_malformed_sdk_logging_does_not_turn_a_paid_answer_into_failure(env, monkeypatch):
    data, gateway = bundle(env), Gateway()
    call = pinned.LiteLLMDelegate.call

    async def logged(*args, **kwargs):
        result = await call(*args, **kwargs)
        logging.getLogger("sdk-boundary").warning("%s %s", "synthetic-only-key")
        return result

    monkeypatch.setattr(pinned.LiteLLMDelegate, "call", logged)
    with data.campaign:
        assert (await ask(router(data, gateway), data.messages)).success
        assert not data.campaign.state.blockers
    with data.campaign:
        assert (await ask(router(data, gateway, dispatch=False), data.messages)).success
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("stage", ["constructor", "reservation", "dispatch"])
async def test_local_setup_failure_never_strands_dispatch_and_closes_owned_client(env, monkeypatch, stage):
    data, gateway = bundle(env), Gateway()
    closed = []
    original_close = pinned.ObservedHTTPHandler.close

    async def close(self):
        closed.append(self)
        await original_close(self)

    def fail(*args, **kwargs):
        raise ValueError("synthetic local setup failure")

    with data.campaign:
        with monkeypatch.context() as patch:
            patch.setattr(pinned.ObservedHTTPHandler, "close", close)
            if stage == "constructor":
                patch.setattr(pinned.ObservedHTTPHandler, "create_client", fail)
            elif stage == "reservation":
                patch.setattr(data.campaign, "reserve", fail)
            else:
                append = data.campaign.append

                def fail_dispatch(kind, **kwargs):
                    return fail() if kind == "dispatch" else append(kind, **kwargs)

                patch.setattr(data.campaign, "append", fail_dispatch)
            with pytest.raises(pinned.LocalFailure, match="local setup"):
                await ask(router(data, gateway), data.messages)
            assert not data.campaign.attempt("case-1")["dispatched"]
            assert not gateway.requests
        assert len(closed) == (0 if stage == "constructor" else 1)
    with data.campaign:
        if stage == "dispatch":
            assert data.campaign.state.blockers == {"reservation:case-1"}
            with pytest.raises(pinned.LocalFailure, match="reservation"):
                await ask(router(data, gateway), data.messages)
            data.campaign.acknowledge("reservation:case-1", "verified synthetic pre-dispatch failure")
        assert (await ask(router(data, gateway), data.messages)).success
    assert len(gateway.requests) == 1
