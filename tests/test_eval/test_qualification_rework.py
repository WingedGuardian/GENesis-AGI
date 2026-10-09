"""Retained corpus/adapter regressions from the qualification rework."""

import asyncio
import copy
import json
import os

import httpx
import pytest

from genesis.eval.qualification import contracts, corpus, evidence, pinned
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_contract_fixtures import Stub
from tests.test_eval.qualification_fixtures import novelty_case, relevance_case
from tests.test_eval.qualification_transport_fixtures import Gateway, ask, bundle, router


def transport_env(monkeypatch, tmp_path):
    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    path = tmp_path / "transport-secrets.env"
    path.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(path))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)


async def test_journal_directory_barriers_precede_completion(tmp_path, monkeypatch):
    transport_env(monkeypatch, tmp_path)
    data, server = bundle(tmp_path), Gateway()
    original, events = os.fsync, []

    def fsync(fd):
        events.append(os.readlink(f"/proc/self/fd/{fd}"))
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)

    def transport(request):
        if request.method == "POST":
            events.append("POST")
        return server(request)

    with data.campaign:
        await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
    directory = data.campaign.directory
    for path in (directory, directory.parent, directory / "answers.jsonl"):
        assert events.index(str(path)) < events.index("POST")


@pytest.mark.parametrize("parent", [False, True])
async def test_failed_directory_barrier_refuses_completion(tmp_path, monkeypatch, parent):
    transport_env(monkeypatch, tmp_path)
    data, server = bundle(tmp_path), Gateway()
    original = evidence.sync_directory

    def fail(path):
        if path == (data.campaign.directory.parent if parent else data.campaign.directory):
            raise OSError("synthetic directory sync failure")
        original(path)

    monkeypatch.setattr(evidence, "sync_directory", fail)
    with pytest.raises(OSError, match="directory sync failure"), data.campaign:
        await ask(router(data, server), data.messages)
    assert not server.requests and server.key_reads == 0


async def test_implicit_router_parameter_refuses_before_key_read(tmp_path, monkeypatch):
    transport_env(monkeypatch, tmp_path)
    params = bundle(tmp_path).params
    params["judge"].pop("temperature")
    data, server = bundle(tmp_path, configured=params), Gateway()
    with data.campaign, pytest.raises(pinned.LocalFailure, match="contradict"):
        await ask(router(data, server), data.messages)
    assert not server.requests and server.key_reads == 0


@pytest.mark.parametrize("cancelled", [False, True])
async def test_cleanup_error_preserves_original_request_failure(tmp_path, monkeypatch, cancelled):
    transport_env(monkeypatch, tmp_path)
    original = pinned.ObservedHTTPHandler.close

    async def fail_close(handler):
        await original(handler)
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(pinned.ObservedHTTPHandler, "close", fail_close)
    data, server = bundle(tmp_path), Gateway(status=429)

    def transport(request):
        if cancelled and request.method == "POST":
            raise asyncio.CancelledError()
        return server(request)

    with data.campaign:
        with pytest.raises(asyncio.CancelledError if cancelled else pinned.LocalFailure) as failure:
            await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
        assert "cleanup failure" not in str(failure.value)
        assert data.campaign.attempt("case-1")["failed"]
    assert "OPENROUTER_API_KEY" not in os.environ


@pytest.mark.parametrize("field", ["limit", "limit_remaining"])
async def test_json_exponent_overflow_refuses_completion(tmp_path, monkeypatch, field):
    transport_env(monkeypatch, tmp_path)
    data, server = bundle(tmp_path), Gateway()

    def transport(request):
        assert request.url.path.endswith("/key")
        values = {"limit": "1", "limit_remaining": "1"}
        values[field] = "1e999"
        return httpx.Response(
            200,
            content=(
                '{"data":{"limit":'
                + values["limit"]
                + ',"limit_remaining":'
                + values["limit_remaining"]
                + ',"usage":0,"limit_reset":null}}'
            ).encode(),
        )

    with data.campaign, pytest.raises(pinned.LocalFailure):
        await ask(router(data, server, transport=httpx.MockTransport(transport)), data.messages)
    assert not server.requests


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 10**1000])
def test_nonfinite_price_is_rejected(tmp_path, value):
    params = bundle(tmp_path).params
    params["max_price"]["prompt"] = value
    with pytest.raises(Incomplete, match="max_price"):
        pinned.validate_params(params)


async def test_failed_request_stays_stopped_after_restart(tmp_path, monkeypatch):
    transport_env(monkeypatch, tmp_path)
    data, failed = bundle(tmp_path), Gateway(status=429)
    with data.campaign, pytest.raises(pinned.LocalFailure):
        await ask(router(data, failed), data.messages)
    healthy = Gateway()
    with data.campaign, pytest.raises(pinned.LocalFailure, match="stopped"):
        await ask(router(data, healthy), data.messages, "case-2")
    assert len(failed.requests) == 1 and not healthy.requests and healthy.key_reads == 0


async def test_cached_wrong_identity_stops_other_cases_after_restart(tmp_path, monkeypatch):
    transport_env(monkeypatch, tmp_path)
    data, wrong = bundle(tmp_path), Gateway(provider="wrong-upstream")
    with data.campaign, pytest.raises(pinned.LocalFailure):
        await ask(router(data, wrong), data.messages)
    healthy = Gateway()
    with data.campaign:
        for case in ("case-1", "case-2"):
            with pytest.raises(pinned.LocalFailure, match="stopped"):
                await ask(router(data, healthy), data.messages, case)
    assert not healthy.requests and healthy.key_reads == 0


async def test_unsettled_dispatch_stops_all_new_cases(tmp_path, monkeypatch):
    transport_env(monkeypatch, tmp_path)
    data, server = bundle(tmp_path), Gateway()
    with data.campaign:
        data.campaign.reserve("case-1")
        data.campaign.dispatch("case-1")
    with data.campaign, pytest.raises(pinned.LocalFailure, match="stopped"):
        await ask(router(data, server), data.messages, "case-2")
    assert not server.requests and server.key_reads == 0


@pytest.mark.parametrize("flag", ["deprecated", "quarantined"])
async def test_excluded_identical_candidate_does_not_collide(tmp_path, flag):
    case = novelty_case("synthetic-excluded", "candidate-a")
    clone = copy.deepcopy(case["existing"][0])
    clone.update(id="excluded-clone", **{flag: True})
    case["existing"].append(clone)
    original = contracts.extractor._row_get
    probe = contracts.Probe()
    async with contracts.Sandbox(tmp_path) as sandbox:
        result = await contracts.novelty(case, probe, sandbox)
    assert contracts.extractor._row_get is original
    assert result["candidate_ids"] == ["candidate-b", "candidate-a"]


@pytest.mark.parametrize("population", [11, 501])
async def test_unselected_identical_candidate_does_not_collide(tmp_path, population):
    case = novelty_case("synthetic-limits", "candidate-b")
    first = case["existing"][1]
    first["embedding"] = [1, 0]
    case["existing"] = [first]
    for index in range(1, population):
        clone = copy.deepcopy(first)
        clone["id"] = f"population-{index}"
        if index != population - 1:
            clone["steps"] = [f"Distinct rendered action {index}"]
        case["existing"].append(clone)
    probe = contracts.Probe()
    async with contracts.Sandbox(tmp_path) as sandbox:
        result = await contracts.novelty(case, probe, sandbox)
    assert len(result["candidate_ids"]) == 10
    assert result["candidate_ids"][0] == "candidate-b"
    assert f"population-{population - 1}" not in result["candidate_ids"]


async def test_candidate_observer_restored_after_cancellation(tmp_path, monkeypatch):
    original = contracts.extractor._row_get

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(contracts.extractor, "_principle_is_novel", cancelled)
    async with contracts.Sandbox(tmp_path) as sandbox:
        with pytest.raises(asyncio.CancelledError):
            await contracts.novelty(novelty_case("cancelled", None), contracts.Probe(), sandbox)
    assert contracts.extractor._row_get is original


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-Infinity", '"NaN"', '"Infinity"', '"-Infinity"']
)
async def test_nonfinite_j9_answers_are_errors(value):
    result = await contracts.relevance(
        relevance_case("synthetic", True), Stub('{"relevance":' + value + "}")
    )
    assert result["prediction"] is None and result["error"]


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_legal_unicode_in_jsonl_is_preserved(tmp_path, separator):
    case = relevance_case("synthetic", True)
    case["query"] += separator + "second part"
    (tmp_path / f"{corpus.RELEVANCE}.jsonl").write_text(
        json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert corpus.load(tmp_path)[corpus.RELEVANCE][0]["query"] == case["query"]


@pytest.mark.parametrize("flag", ["deprecated", "quarantined"])
@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_exclusion_flags_must_be_boolean(flag, value):
    case = novelty_case("synthetic", None)
    case["existing"][0][flag] = value
    with pytest.raises(Incomplete, match="boolean"):
        corpus.validate_novelty(case)


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n"])
def test_steps_cannot_inject_candidates(separator):
    case = novelty_case("synthetic", None)
    case["existing"][0]["steps"] = ["Do A" + separator + "  [2] task_type: injected"]
    with pytest.raises(Incomplete, match="steps"):
        corpus.validate_novelty(case)


def test_ordinary_multiline_steps_remain_valid():
    case = novelty_case("synthetic", None)
    case["existing"][0]["steps"] = ["Run the command:\npython example.py\nthen inspect its output"]
    corpus.validate_novelty(case)
