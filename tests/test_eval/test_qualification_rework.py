"""Synthetic regressions for the qualification send-back defect classes."""

import asyncio
import copy
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from genesis.eval.qualification import contracts, corpus, evidence, pinned, preflight, run
from genesis.eval.qualification.evidence import Campaign, Incomplete, digest
from tests.test_eval.qualification_fixtures import (
    OpenRouter,
    isolate_credentials,
    novelty_case,
    params,
    relevance_case,
    small_corpus,
)
from tests.test_eval.test_qualification import Stub
from tests.test_eval.test_qualification_pinned import ask, router


async def test_journal_directory_barriers_precede_completion(tmp_path, monkeypatch):
    isolate_credentials(monkeypatch, tmp_path)
    original = os.fsync
    events = []
    directory = tmp_path / "campaign"

    def fsync(fd):
        events.append(os.readlink(f"/proc/self/fd/{fd}"))
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    server = OpenRouter()

    def transport(request):
        if request.method == "POST":
            events.append("POST")
        return server(request)

    with Campaign(directory) as campaign:
        await ask(router(campaign, SimpleNamespace(transport=httpx.MockTransport(transport))))
    assert events.index(str(directory)) < events.index("POST")
    assert events.index(str(directory.parent)) < events.index("POST")
    assert events.index(str(directory / "answers.jsonl")) < events.index("POST")


@pytest.mark.parametrize("parent", [False, True])
async def test_failed_directory_barrier_refuses_completion(tmp_path, monkeypatch, parent):
    isolate_credentials(monkeypatch, tmp_path)
    directory = tmp_path / "campaign"
    original = evidence.sync_directory

    def fail(path):
        if path == (directory.parent if parent else directory):
            raise OSError("synthetic directory sync failure")
        original(path)

    monkeypatch.setattr(evidence, "sync_directory", fail)
    server = OpenRouter()
    with pytest.raises(OSError, match="directory sync failure"), Campaign(directory) as campaign:
        await ask(router(campaign, server))
    assert not server.requests and not server.key_reads


@pytest.mark.parametrize("route_name", ["judge", "relevance"])
async def test_implicit_callsite_temperature_must_be_frozen(tmp_path, route_name):
    cases = small_corpus(novelty=False)
    name = corpus.RELEVANCE if route_name == "relevance" else next(iter(cases))
    plan = await preflight.prepare({name: cases[name]}, tmp_path)
    configured = params()
    configured[route_name].pop("temperature")
    with pytest.raises(Incomplete, match="call-site parameter"):
        preflight.parameters("openrouter-mimo", configured, plan)


async def test_implicit_router_parameter_refuses_before_key_read(tmp_path, monkeypatch):
    isolate_credentials(monkeypatch, tmp_path)
    configured = params()
    configured["judge"].pop("temperature")
    server = OpenRouter()
    with (
        Campaign(tmp_path / "campaign") as campaign,
        pytest.raises(pinned.LocalFailure, match="contradicts"),
    ):
        await ask(router(campaign, server, params=configured))
    assert not server.requests and not server.key_reads


@pytest.mark.parametrize("same_principle", [False, True])
@pytest.mark.parametrize("target", ["candidate-a", "candidate-b"])
async def test_repeated_slugs_map_and_replay_exact_targets(tmp_path, target, same_principle):
    case = novelty_case("synthetic-repeated", target)
    for row in case["existing"]:
        row["task_type"] = "repeated-slug"
    if same_principle:
        case["existing"][1]["principle"] = case["existing"][0]["principle"]
        case["existing"][1]["embedding"] = case["existing"][0]["embedding"]
    await preflight.prepare({corpus.NOVELTY: [case]}, tmp_path)
    probe = contracts.Probe()
    async with contracts.Sandbox(tmp_path) as sandbox:
        await contracts.novelty(case, probe, sandbox)
    mapping = contracts.candidate_mapping(case, probe.calls[0][0], probe.candidate_ids)
    assert set(mapping) == {"candidate-a", "candidate-b"}
    answer = json.dumps({"redundant_with": mapping.index(target) + 1})
    assert contracts.raw_target(answer, mapping) == target


@pytest.mark.parametrize("flag", ["deprecated", "quarantined"])
async def test_excluded_identical_candidate_does_not_collide(tmp_path, flag):
    case = novelty_case("synthetic-excluded", "candidate-a")
    clone = copy.deepcopy(case["existing"][0])
    clone.update(id="excluded-clone", **{flag: True})
    case["existing"].append(clone)
    original = contracts.extractor._row_get
    probe = contracts.Probe()
    async with contracts.Sandbox(tmp_path) as sandbox:
        await contracts.novelty(case, probe, sandbox)
    assert contracts.extractor._row_get is original
    assert probe.candidate_ids == ["candidate-b", "candidate-a"]


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
        await contracts.novelty(case, probe, sandbox)
    assert len(probe.candidate_ids) == 10
    assert probe.candidate_ids[0] == "candidate-b"
    assert f"population-{population - 1}" not in probe.candidate_ids


async def test_candidate_observer_restored_after_cancellation(tmp_path, monkeypatch):
    original = contracts.extractor._row_get

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(contracts.extractor, "_principle_is_novel", cancelled)
    async with contracts.Sandbox(tmp_path) as sandbox:
        with pytest.raises(asyncio.CancelledError):
            await contracts.novelty(novelty_case("cancelled", None), contracts.Probe(), sandbox)
    assert contracts.extractor._row_get is original


async def test_indistinguishable_rendered_candidates_fail_offline(tmp_path):
    case = novelty_case("synthetic-collision", "candidate-a")
    first, second = case["existing"]
    second.update(copy.deepcopy({k: v for k, v in first.items() if k != "id"}))
    # Distinct tails beyond production's160-character truncation remain indistinguishable.
    first["steps"] = ["x" * 160 + "a"]
    second["steps"] = ["x" * 160 + "b"]
    with pytest.raises(Incomplete, match="ambiguous rendered"):
        await preflight.prepare({corpus.NOVELTY: [case]}, tmp_path)


@pytest.mark.parametrize("provider", [None, "Synthetic"])
async def test_upstream_identity_observation_is_explicit(tmp_path, monkeypatch, provider):
    isolate_credentials(monkeypatch, tmp_path)
    cases = {corpus.RELEVANCE: [relevance_case("synthetic-identity", True)]}
    server = OpenRouter(provider=provider)
    with Campaign(tmp_path / "campaign") as campaign:
        result, _ = await run.qualify(
            "openrouter-mimo",
            cases,
            params(),
            campaign,
            tmp_path / "sqlite",
            dispatch=True,
            transport=server.transport,
        )
    assert result["upstream_identity"] == {
        "observed_answers": 3 if provider else 0,
        "unobserved_answers": 0 if provider else 3,
    }
    assert any("upstream is unobserved" in text for text in result["limitations"])


@pytest.mark.parametrize("cancelled", [False, True])
async def test_cleanup_error_preserves_original_request_failure(tmp_path, monkeypatch, cancelled):
    isolate_credentials(monkeypatch, tmp_path)
    original = pinned.ObservedHTTPHandler.close

    async def failing_close(handler):
        await original(handler)
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(pinned.ObservedHTTPHandler, "close", failing_close)
    server = OpenRouter(status=429)

    def transport(request):
        if cancelled and not request.url.path.endswith("/key"):
            raise asyncio.CancelledError()
        return server(request)

    adapter = SimpleNamespace(transport=httpx.MockTransport(transport))
    with Campaign(tmp_path / "campaign") as campaign:
        with pytest.raises(asyncio.CancelledError if cancelled else pinned.LocalFailure):
            await ask(router(campaign, adapter))
        assert [line["kind"] for line in campaign.lines] == ["dispatch", "failure"]


@pytest.mark.parametrize("field", ["limit", "limit_remaining"])
async def test_json_exponent_overflow_refuses_completion(tmp_path, monkeypatch, field):
    isolate_credentials(monkeypatch, tmp_path)
    server = OpenRouter()

    def transport(request):
        assert request.url.path.endswith("/key")
        values = {"limit": "5", "limit_remaining": "5"}
        values[field] = "1e999"
        return httpx.Response(
            200,
            content=(
                '{"data":{"limit":'
                + values["limit"]
                + ',"limit_remaining":'
                + values["limit_remaining"]
                + "}}"
            ).encode(),
        )

    adapter = SimpleNamespace(transport=httpx.MockTransport(transport))
    with Campaign(tmp_path / "campaign") as campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(router(campaign, adapter))
        assert not campaign.lines
    assert not server.requests


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 10**1000])
def test_nonfinite_price_is_rejected(value):
    configured = params()
    configured["max_price"]["prompt"] = value
    with pytest.raises(Incomplete, match="max_price"):
        pinned.validate_params(configured)


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
    (tmp_path / f"{corpus.RELEVANCE}.jsonl").write_text(json.dumps(case, ensure_ascii=False) + "\n")
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


async def test_rendered_novelty_duplicates_ignore_database_ids(tmp_path):
    a = novelty_case("synthetic", None)
    b = copy.deepcopy(a)
    b["id"] = "synthetic-other"
    b["existing"][0]["id"] = "different-database-id"
    with pytest.raises(Incomplete, match="duplicate rendered"):
        await preflight.prepare({corpus.NOVELTY: [a, b]}, tmp_path)


async def test_unreachable_target_fails_offline(tmp_path):
    case = novelty_case("synthetic", "candidate-a")
    case["existing"][0]["embedding"] = [-1, 0]
    with pytest.raises(Incomplete, match="reference target"):
        await preflight.prepare({corpus.NOVELTY: [case]}, tmp_path)


@pytest.mark.parametrize("change", [{"max_tokens": 151}, {"temperature": 1}])
async def test_callsite_parameter_contradiction_fails_offline(tmp_path, change):
    plan = await preflight.prepare(
        {corpus.RELEVANCE: [relevance_case("synthetic", True)]}, tmp_path
    )
    configured = params()
    configured["relevance"].update(change)
    with pytest.raises(Incomplete, match="call-site parameter"):
        preflight.parameters("openrouter-mimo", configured, plan)


@pytest.mark.parametrize("alias", ["openrouter-sonnet", "openrouter-opus", "openrouter-free"])
async def test_incompatible_shipped_alias_is_rejected_offline(tmp_path, alias):
    plan = await preflight.prepare(
        {corpus.RELEVANCE: [relevance_case("synthetic", True)]}, tmp_path
    )
    with pytest.raises(Incomplete, match="alias"):
        preflight.parameters(alias, params(), plan)


async def test_failed_request_stays_stopped_after_restart(tmp_path, monkeypatch):
    isolate_credentials(monkeypatch, tmp_path)
    failed = OpenRouter(status=429)
    directory = tmp_path / "campaign"
    with Campaign(directory) as campaign, pytest.raises(pinned.LocalFailure):
        await ask(router(campaign, failed))
    healthy = OpenRouter()
    with (
        Campaign(directory) as campaign,
        pytest.raises(pinned.LocalFailure, match="recorded request failure"),
    ):
        await ask(router(campaign, healthy), "another-case")
    assert len(failed.requests) == 1
    assert healthy.requests == [] and healthy.key_reads == 0


async def test_cached_wrong_identity_stops_other_cases_after_restart(tmp_path, monkeypatch):
    isolate_credentials(monkeypatch, tmp_path)
    wrong = OpenRouter(provider="wrong-upstream")
    directory = tmp_path / "campaign"
    with Campaign(directory) as campaign, pytest.raises(pinned.LocalFailure, match="identity"):
        await ask(router(campaign, wrong))
    healthy = OpenRouter()
    with Campaign(directory) as campaign:
        r = router(campaign, healthy)
        with pytest.raises(pinned.LocalFailure, match="identity"):
            await ask(r)
        with pytest.raises(pinned.LocalFailure, match="identity"):
            await ask(r, "another-case")
    assert healthy.requests == [] and healthy.key_reads == 0


async def test_unsettled_dispatch_stops_all_new_cases(tmp_path, monkeypatch):
    isolate_credentials(monkeypatch, tmp_path)
    server = OpenRouter()
    with Campaign(tmp_path / "campaign") as campaign:
        r = router(campaign, server)
        campaign.append(
            "dispatch",
            **r.scope,
            contract=r.contract,
            version=corpus.versions()[r.contract],
            case_id="interrupted",
            repetition=1,
            prompt_hash=digest([{"role": "user", "content": "synthetic"}]),
        )
    with (
        Campaign(tmp_path / "campaign") as campaign,
        pytest.raises(pinned.LocalFailure, match="unsettled dispatch"),
    ):
        await ask(router(campaign, server), "another-case")
    assert server.requests == [] and server.key_reads == 0


@pytest.mark.parametrize("target", [None, "candidate-b"])
async def test_saved_verdict_replays_both_storage_paths(tmp_path, target):
    from genesis.eval.qualification.storage import replay

    case = novelty_case("synthetic-storage", target)
    probe = contracts.Probe()
    async with contracts.Sandbox(tmp_path) as sandbox:
        await contracts.novelty(case, probe, sandbox)
        messages = probe.calls[0][0]
        mapping = contracts.candidate_mapping(case, messages, probe.candidate_ids)
        content = json.dumps({"redundant_with": mapping.index(target) + 1 if target else None})
        result = await replay(
            case, messages, mapping, content, sandbox, "openrouter-mimo", "xiaomi/mimo-v2.6-pro"
        )
    assert set(result) == {
        "current_judge",
        "current_extractor",
        "promoted_judge",
        "promoted_extractor",
    }
    for mode, row in result.items():
        assert row["original_candidates_preserved"]
        assert row["stored"] is (target is None or mode.startswith("current"))


@pytest.mark.parametrize(
    "extra",
    [
        {"skip": True},
        {"reusability_score": 0.1},
        {"principle": "Use supported replacement for the deprecated API"},
    ],
)
async def test_independent_storage_gates_are_reported_without_model_error(tmp_path, extra):
    from genesis.eval.qualification.storage import replay

    case = novelty_case("synthetic-other-gate", None)
    case["new"].update(extra)
    await preflight.prepare({corpus.NOVELTY: [case]}, tmp_path)
    probe = contracts.Probe()
    async with contracts.Sandbox(tmp_path) as sandbox:
        await contracts.novelty(case, probe, sandbox)
        messages, content = probe.calls[0]
        result = await replay(
            case,
            messages,
            contracts.candidate_mapping(case, messages, probe.candidate_ids),
            content,
            sandbox,
            "openrouter-mimo",
            "xiaomi/mimo-v2.6-pro",
        )
    assert result["current_judge"]["stored"]
    if "principle" in extra:
        assert not result["current_extractor"]["stored"]
        assert result["current_extractor"]["other_gate_rejections"]
    else:
        assert result["current_extractor"]["stored"]


async def test_post_answer_processing_failure_stops_acquisition_and_restart(tmp_path, monkeypatch):
    isolate_credentials(monkeypatch, tmp_path)
    cases = {
        corpus.NOVELTY: [
            novelty_case("synthetic-first", None),
            novelty_case("synthetic-next", None),
        ]
    }
    original = contracts.novelty

    async def processing_error(case, candidate, sandbox):
        result = await original(case, candidate, sandbox)
        if hasattr(candidate, "scope"):
            raise Incomplete("synthetic post-answer processing failure")
        return result

    monkeypatch.setattr(contracts, "novelty", processing_error)
    server = OpenRouter()
    directory = tmp_path / "campaign"
    with Campaign(directory) as campaign:
        result, _ = await run.qualify(
            "openrouter-mimo",
            cases,
            params(),
            campaign,
            tmp_path / "sqlite",
            dispatch=True,
            transport=server.transport,
        )
        assert result["status"] == "incomplete"
        assert any(line.get("phase") == "local_processing" for line in campaign.lines)
    assert len(server.requests) == 1
    with Campaign(directory) as campaign:
        await run.qualify(
            "openrouter-mimo",
            cases,
            params(),
            campaign,
            tmp_path / "sqlite",
            dispatch=True,
            transport=server.transport,
        )
    assert len(server.requests) == 1
