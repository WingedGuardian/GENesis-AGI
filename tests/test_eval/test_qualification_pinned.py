"""PinnedRouter, spend bounds and campaign evidence, against a mocked OpenRouter.

Every request goes through the production ``LiteLLMDelegate`` into an
``httpx.MockTransport``; no credential is real and nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import os

import httpx
import litellm
import pytest

from genesis.eval.qualification import corpus, pinned, run
from genesis.eval.qualification.__main__ import main
from genesis.eval.qualification.evidence import Campaign, Incomplete
from genesis.eval.rubrics import list_rubrics
from tests.test_eval.qualification_fixtures import (
    KEY,
    MODELS,
    OpenRouter,
    isolate_credentials,
    params,
    small_corpus,
    write_corpus,
)

RUBRIC = list_rubrics()[0].name
MESSAGES = [{"role": "user", "content": "Synthetic question"}]


@pytest.fixture
def env(monkeypatch, tmp_path):
    isolate_credentials(monkeypatch, tmp_path)
    return tmp_path


def router(campaign, server, *, alias="openrouter-mimo", cap=100, dispatch=True, **kw):
    result = pinned.PinnedRouter(
        campaign,
        alias=alias,
        params=kw.pop("params", params()),
        cap=cap,
        dispatch=dispatch,
        transport=server.transport,
        **kw,
    )
    result.bind(RUBRIC, 1, case_id="case-1")
    return result


async def ask(r, case_id="case-1", **kwargs):
    r.bind(RUBRIC, 1, case_id=case_id)
    return await r.route_call("judge", MESSAGES, temperature=0.0, **kwargs)


def kinds(campaign):
    return [line["kind"] for line in campaign.lines]


async def test_pinned_wire_request_and_answer_evidence(env):
    server = OpenRouter(answer=lambda _p: '{"score": 1}')
    callbacks = {n: list(getattr(litellm, n)) for n in ("callbacks", "success_callback")}
    with Campaign(env / "campaign") as campaign:
        result = await ask(router(campaign, server))
    assert result.success and result.content == '{"score": 1}'
    assert result.provider_used == "openrouter-mimo"
    (request,) = server.requests
    assert request["headers"]["authorization"] == f"Bearer {KEY}"
    assert request["body"] == {
        "model": MODELS["openrouter-mimo"],
        "messages": MESSAGES,
        "temperature": 0.0,
        "max_tokens": 400,
        "usage": {"include": True},
        "provider": {
            "only": ["Synthetic"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": 1, "completion": 2},
        },
    }
    assert kinds(campaign) == ["dispatch", "answer"]
    answer = campaign.lines[1]
    assert answer["prompt_hash"] and answer["version"] == corpus.versions()[RUBRIC]
    assert answer["generation_id"] == "gen-1" and answer["usage"]["cost"] == 0.0001
    assert callbacks == {n: list(getattr(litellm, n)) for n in callbacks}
    assert os.environ.get("OPENROUTER_API_KEY") is None  # the lent key was returned


async def test_paid_answer_is_reused_offline_without_a_key(env, monkeypatch):
    server = OpenRouter(answer=lambda _p: '{"score": 1}')
    with Campaign(env / "campaign") as campaign:
        await ask(router(campaign, server))
    monkeypatch.delenv(pinned.KEY_ENV)
    offline = OpenRouter()
    with Campaign(env / "campaign") as campaign:
        for dispatch in (True, False):
            result = await ask(router(campaign, offline, dispatch=dispatch))
            assert result.content == '{"score": 1}'
        with pytest.raises(pinned.LocalFailure, match="unanswered"):
            await ask(router(campaign, offline, dispatch=False), case_id="case-2")
    assert offline.requests == [] and offline.key_reads == 0


async def test_request_cap_counts_dispatch_lines_across_restarts(env):
    server = OpenRouter(answer=lambda _p: '{"score": 1}')
    with Campaign(env / "campaign") as campaign:
        r = router(campaign, server, cap=2)
        await ask(r, "case-1")
        # A crash after dispatch and before the answer: the request still counts.
        campaign.append("dispatch", **r.scope, contract=RUBRIC, case_id="case-2")
    with Campaign(env / "campaign") as campaign:
        r = router(campaign, server, cap=2)
        assert r.dispatched == 2
        with pytest.raises(pinned.LocalFailure, match="unsettled dispatch"):
            await ask(r, "case-2")
        with pytest.raises(pinned.LocalFailure, match="unsettled dispatch"):
            await ask(r, "case-3")  # tripped: no further attempt
        assert (await ask(r, "case-1")).content  # paid answers still rescore
    assert len(server.requests) == 1
    assert pinned.request_cap(750) == 2250


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        ("missing", "dedicated"),
        ("production_env", "production"),
        ("production_secrets", "production"),
        ("unlimited", "no credit limit"),
        ("exhausted", "exhausted"),
        ("unauthorized", "could not read"),
    ],
)
async def test_dedicated_limited_key_is_required_before_any_completion(
    env, monkeypatch, setup, match
):
    server = OpenRouter()
    if setup == "missing":
        monkeypatch.delenv(pinned.KEY_ENV)
    elif setup == "production_env":
        monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    elif setup == "production_secrets":
        (env / "secrets.env").write_text(f"API_KEY_OPENROUTER={KEY}\n")
    elif setup == "unlimited":
        server.limit = None
    elif setup == "exhausted":
        server.limit = 0
    original = server.__call__

    def unauthorized(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(401, json={"error": "synthetic"})
        return original(request)

    transport = server if setup != "unauthorized" else unauthorized
    with Campaign(env / "campaign") as campaign:
        r = pinned.PinnedRouter(
            campaign,
            alias="openrouter-mimo",
            params=params(),
            cap=10,
            dispatch=True,
            transport=httpx.MockTransport(transport),
        )
        with pytest.raises(pinned.LocalFailure, match=match):
            await ask(r)
        assert kinds(campaign) == []
    assert server.requests == []


@pytest.mark.parametrize("mode", ["throttle", "timeout", "garbled"])
async def test_failed_request_is_recorded_and_trips_the_router(env, monkeypatch, mode):
    server = OpenRouter(status=429 if mode == "throttle" else 200)
    original = server.__call__

    def transport(request):
        if mode == "timeout" and not request.url.path.endswith("/key"):
            server.requests.append(request)
            raise httpx.ReadTimeout("synthetic timeout")
        if mode == "garbled" and not request.url.path.endswith("/key"):
            server.requests.append(request)
            return httpx.Response(200, content=b"not json")
        return original(request)

    with Campaign(env / "campaign") as campaign:
        r = pinned.PinnedRouter(
            campaign,
            alias="openrouter-mimo",
            params=params(),
            cap=10,
            dispatch=True,
            transport=httpx.MockTransport(transport),
        )
        with pytest.raises(pinned.LocalFailure):
            await ask(r, "case-1")
        with pytest.raises(pinned.LocalFailure):
            await ask(r, "case-2")
        assert kinds(campaign) == ["dispatch", "failure"]
    assert len(server.requests) == 1


async def test_cancellation_is_recorded_and_propagates(env):
    def transport(request):
        if request.url.path.endswith("/key"):
            return OpenRouter()(request)
        raise asyncio.CancelledError()

    with Campaign(env / "campaign") as campaign:
        r = pinned.PinnedRouter(
            campaign,
            alias="openrouter-mimo",
            params=params(),
            cap=10,
            dispatch=True,
            transport=httpx.MockTransport(transport),
        )
        with pytest.raises(asyncio.CancelledError):
            await ask(r)
        assert kinds(campaign) == ["dispatch", "failure"]


@pytest.mark.parametrize("field", ["model", "provider"])
async def test_served_identity_mismatch_is_kept_but_never_scored(env, field):
    server = OpenRouter(provider="Other" if field == "provider" else "Synthetic")
    if field == "model":
        original = server.__call__

        def swap(request):
            response = original(request)
            if request.url.path.endswith("/key"):
                return response
            data = json.loads(response.content)
            return httpx.Response(200, json={**data, "model": "other/model"})

        transport = httpx.MockTransport(swap)
    else:
        transport = server.transport
    with Campaign(env / "campaign") as campaign:
        r = pinned.PinnedRouter(
            campaign,
            alias="openrouter-mimo",
            params=params(),
            cap=10,
            dispatch=True,
            transport=transport,
        )
        for case in ("case-1", "case-2"):
            with pytest.raises(pinned.LocalFailure):
                await ask(r, case)
        assert kinds(campaign) == ["dispatch", "answer"]


async def test_provider_defaults_that_change_the_wire_request_have_zero_egress(env):
    config = pinned.routing()
    cfg = dataclasses.replace(config.providers["openrouter-mimo"], params={"top_p": 0.5})
    config = dataclasses.replace(config, providers={**config.providers, "openrouter-mimo": cfg})
    server = OpenRouter()
    with Campaign(env / "campaign") as campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(router(campaign, server, config=config))
        assert kinds(campaign) == ["dispatch", "failure"]
    assert server.requests == []


async def test_global_litellm_observers_block_before_the_key_is_read(env, monkeypatch):
    monkeypatch.setattr(litellm, "success_callback", ["synthetic"])
    server = OpenRouter()
    with (
        Campaign(env / "campaign") as campaign,
        pytest.raises(pinned.LocalFailure, match="observers"),
    ):
        await ask(router(campaign, server))
    assert server.key_reads == 0 and server.requests == []


async def test_call_site_parameter_contradicting_params_is_refused(env):
    configured = params()
    configured["judge"]["temperature"] = 0.5
    server = OpenRouter()
    with Campaign(env / "campaign") as campaign:
        with pytest.raises(pinned.LocalFailure, match="temperature"):
            await ask(router(campaign, server, params=configured))
        with pytest.raises(pinned.LocalFailure, match="rotation"):
            await ask(router(campaign, server), chain_offset=1)
    assert server.requests == []


@pytest.mark.parametrize(
    "change",
    [
        {"upstream": ""},
        {"max_price": {"prompt": 1}},
        {"max_price": {"prompt": True, "completion": 1}},
        {"judge": {"temperature": 0.0}},
        {"judge": {"max_tokens": True}},
        {"relevance": {"max_tokens": 10, "temperature": 2.5}},
        {"novelty": {"max_tokens": 10, "top_p": 1.1}},
        {"novelty": {"max_tokens": 10, "seed": 1.5}},
        {"novelty": {"max_tokens": 10, "reasoning": {"max_tokens": -1}}},
        {"novelty": {"max_tokens": 10, "provider": {"only": ["x"]}}},
        {"extra": 1},
    ],
)
def test_invalid_params_rejected(change):
    with pytest.raises(Incomplete):
        pinned.validate_params({**params(), **change})


@pytest.mark.parametrize("alias", ["deepseek-chat", "no-such-alias"])
def test_only_openrouter_aliases_resolve(alias):
    with pytest.raises(Incomplete):
        pinned.resolve(alias)


async def test_credentials_are_redacted_from_answers_and_logs(env, caplog):
    server = OpenRouter(answer=lambda _p: f"echo {KEY}")
    with Campaign(env / "campaign") as campaign:
        await ask(router(campaign, server))
    text = (env / "campaign" / "answers.jsonl").read_text()
    assert KEY not in text and "[REDACTED]" in text
    with caplog.at_level(logging.WARNING), pinned.private_logs():
        logging.getLogger("synthetic").warning("leak %s", KEY)
    assert KEY not in caplog.text and "[REDACTED]" in caplog.text
    assert logging.getLogRecordFactory() is logging.LogRecord


def test_campaign_files_private_locked_and_torn_tail_discarded(env):
    directory = env / "campaign"
    with Campaign(directory) as campaign:
        campaign.append("dispatch", alias="a")
        with pytest.raises(Incomplete, match="in use"), Campaign(directory):
            pass
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    answers = directory / "answers.jsonl"
    assert oct(answers.stat().st_mode & 0o777) == "0o600"
    with answers.open("ab") as stream:
        stream.write(b'{"kind": "answer", "alias"')
    with Campaign(directory) as campaign:
        assert campaign.torn_tail_discarded and kinds(campaign) == ["dispatch"]
    assert answers.read_bytes().endswith(b"\n")
    with answers.open("ab") as stream:
        stream.write(b'{"kind": "other"}\n')
    with pytest.raises(Incomplete, match="line 2"), Campaign(directory):
        pass


def test_campaign_directory_must_be_private(env):
    directory = env / "open"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(Incomplete, match="0700"), Campaign(directory):
        pass


@pytest.fixture
def small_floors(monkeypatch):
    monkeypatch.setitem(corpus.DEFAULT_FLOOR, False, 1)
    monkeypatch.setitem(corpus.DEFAULT_FLOOR, True, 1)
    monkeypatch.setitem(corpus.FLOORS[corpus.NOVELTY], False, 1)
    monkeypatch.setitem(corpus.FLOORS[corpus.NOVELTY], True, 1)


def cli(env, server, monkeypatch, *args):
    monkeypatch.setattr(
        run, "PinnedRouter", functools.partial(pinned.PinnedRouter, transport=server.transport)
    )
    params_file = env / "params.json"
    params_file.write_text(json.dumps({alias: params() for alias in MODELS}))
    common = ["--corpus", str(env / "corpus"), "--temp-root", str(env / "sqlite")]
    common += ["--campaign", str(env / "campaign"), "--params", str(params_file)]
    return main([*args, *common])


def test_cli_run_refuses_incomplete_coverage_without_requests(env, monkeypatch, capsys):
    write_corpus(env / "corpus", small_corpus())
    server = OpenRouter()
    assert cli(env, server, monkeypatch, "run", "--alias", "openrouter-mimo") == 2
    assert "coverage" in json.loads(capsys.readouterr().out)["reason"]
    assert server.requests == [] and not (env / "campaign").exists()


def test_cli_run_qualifies_then_report_rescores_offline(env, monkeypatch, capsys, small_floors):
    write_corpus(env / "corpus", small_corpus())
    server = OpenRouter()
    assert cli(env, server, monkeypatch, "run", "--alias", "openrouter-mimo") == 0
    result = json.loads(capsys.readouterr().out)
    cases = sum(len(rows) for rows in small_corpus().values())
    assert len(server.requests) == cases * 3
    assert result["routes"] == {"judge": "pass", "novelty": "pass"}
    assert result["requests"] == {
        "cap": cases * 3,
        "dispatched": cases * 3,
        "answered": cases * 3,
        "failed": 0,
        "truncated": 0,
    }
    assert result["key"]["limit"] == 5
    rubric = result["contracts"][RUBRIC]["repetitions"][0]
    assert rubric["classes"]["true"] == {"denominator": 1, "correct": 1, "agreement": 1.0}

    monkeypatch.delenv(pinned.KEY_ENV)
    offline = OpenRouter()
    assert cli(env, offline, monkeypatch, "report", "--alias", "openrouter-mimo") == 0
    again = json.loads(capsys.readouterr().out)
    assert again["contracts"] == result["contracts"] and again["key"] is None
    assert offline.requests == [] and offline.key_reads == 0


def test_cli_rerun_after_interruption_stays_stopped(
    env, monkeypatch, capsys, small_floors
):
    write_corpus(env / "corpus", small_corpus())
    flaky = OpenRouter()
    original = flaky.__call__

    def fail_fifth(request):
        if len(flaky.requests) == 4 and not request.url.path.endswith("/key"):
            flaky.requests.append(request)
            return httpx.Response(502, json={"error": "synthetic"})
        return original(request)

    flaky.__class__ = type("Flaky", (OpenRouter,), {"__call__": lambda s, r: fail_fifth(r)})
    assert cli(env, flaky, monkeypatch, "run", "--alias", "openrouter-mimo") == 2
    first = json.loads(capsys.readouterr().out)
    assert first["requests"]["failed"] == 1 and len(flaky.requests) == 5
    server = OpenRouter()
    assert cli(env, server, monkeypatch, "run", "--alias", "openrouter-mimo") == 2
    assert server.requests == [] and server.key_reads == 0
    assert json.loads(capsys.readouterr().out)["requests"]["dispatched"] == 5


def test_cli_report_against_pairs_two_aliases(env, monkeypatch, capsys, small_floors):
    write_corpus(env / "corpus", small_corpus())
    assert cli(env, OpenRouter(), monkeypatch, "run", "--alias", "openrouter-mimo") == 0

    def always_pass(prompt):
        if "task_type:" in prompt:
            return '{"redundant_with": null}'
        if "judging whether a recalled memory is relevant" in prompt:
            return '{"relevance": 1}'
        return '{"score": 1}'

    flash = OpenRouter(answer=always_pass)
    assert cli(env, flash, monkeypatch, "run", "--alias", "openrouter-deepseek-flash") == 1
    capsys.readouterr()
    code = cli(
        env,
        OpenRouter(),
        monkeypatch,
        "report",
        "--alias",
        "openrouter-mimo",
        "--against",
        "openrouter-deepseek-flash",
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["status"] == "fail"
    assert result["eligible_by_route"] == {
        "judge": ["openrouter-mimo"],
        "novelty": ["openrouter-mimo"],
    }
    rubric = result["paired"][RUBRIC][0]
    assert rubric == {
        "repetition": 1,
        "cases": 2,
        "valid_pairs": 2,
        "matching_predictions": 1,
        "both_correct": 1,
        "left_only_correct": 1,
        "right_only_correct": 0,
        "neither_correct": 0,
        "error_or_unscored_pairs": 0,
    }
    flash_body = flash.requests[0]["body"]
    assert flash_body["model"] == MODELS["openrouter-deepseek-flash"]


def test_cli_report_marks_changed_prompts_unanswered(env, monkeypatch, capsys, small_floors):
    write_corpus(env / "corpus", small_corpus())
    assert cli(env, OpenRouter(), monkeypatch, "run", "--alias", "openrouter-mimo") == 0
    capsys.readouterr()
    cases = small_corpus()
    cases[RUBRIC][0]["actual"] += " (edited)"
    write_corpus(env / "corpus", cases)
    assert cli(env, OpenRouter(), monkeypatch, "report", "--alias", "openrouter-mimo") == 2
    rep = json.loads(capsys.readouterr().out)["contracts"][RUBRIC]["repetitions"][0]
    assert rep["unscored"] == 1 and rep["unscored_reasons"] == ["unanswered"]


async def test_malformed_answer_is_a_model_error_not_a_local_failure(env, small_floors):
    cases = {RUBRIC: small_corpus()[RUBRIC]}
    directory = write_corpus(env / "corpus", cases)
    server = OpenRouter(answer=lambda _p: "not a judgment")
    with Campaign(env / "campaign") as campaign:
        report, records = await run.qualify(
            "openrouter-mimo",
            corpus.load(directory),
            params(),
            campaign,
            env / "sqlite",
            dispatch=True,
            transport=server.transport,
        )
    rep = report["contracts"][RUBRIC]["repetitions"][0]
    assert rep["status"] == "fail" and rep["errors"] == 2 and rep["unscored"] == 0
    assert report["routes"]["novelty"] == "incomplete"  # staged: novelty not labelled yet


async def test_answer_observed_before_a_late_cancellation_is_kept(env, monkeypatch):
    original = pinned.LiteLLMDelegate.call

    async def cancelled_after(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(pinned.LiteLLMDelegate, "call", cancelled_after)
    server = OpenRouter(answer=lambda _p: '{"score": 1}')
    with Campaign(env / "campaign") as campaign:
        with pytest.raises(asyncio.CancelledError):
            await ask(router(campaign, server))
        assert kinds(campaign) == ["dispatch", "answer"]
    with Campaign(env / "campaign") as campaign:
        assert (await ask(router(campaign, OpenRouter(), dispatch=False))).content


@pytest.mark.parametrize("alias", ["openrouter-mimo", "deepseek-chat"])
def test_cli_invalid_params_or_alias_create_no_campaign(env, monkeypatch, capsys, alias):
    write_corpus(env / "corpus", small_corpus())
    (env / "params.json").write_text(json.dumps({alias: {"upstream": "x"}}))
    code = main(
        [
            "report",
            "--alias",
            alias,
            "--corpus",
            str(env / "corpus"),
            "--temp-root",
            str(env / "sqlite"),
            "--campaign",
            str(env / "campaign"),
            "--params",
            str(env / "params.json"),
        ]
    )
    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "incomplete"
    assert not (env / "campaign").exists()


async def test_client_retry_after_a_dropped_connection_never_sends_twice(env):
    """MEASURED: LiteLLM's HTTP client retries a dropped connection once by itself."""
    server = OpenRouter(answer=lambda _p: '{"score": 1}')
    original = server.__call__
    completions = []

    def dropped_once(request):
        if request.url.path.endswith("/key"):
            return original(request)
        completions.append(request)
        if len(completions) == 1:
            raise httpx.RemoteProtocolError("synthetic dropped connection")
        return original(request)

    with Campaign(env / "campaign") as campaign:
        r = pinned.PinnedRouter(
            campaign,
            alias="openrouter-mimo",
            params=params(),
            cap=10,
            dispatch=True,
            transport=httpx.MockTransport(dropped_once),
        )
        with pytest.raises(pinned.LocalFailure):
            await ask(r)
        assert campaign.lines[-1]["error"] == "a repeated POST was refused"
    assert len(completions) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "synthetic upstream error", "code": 502}},
        {"id": "gen-1", "model": "xiaomi/mimo-v2.6-pro", "choices": []},
        {
            "id": "gen-1",
            "model": "xiaomi/mimo-v2.6-pro",
            "choices": [{"index": 0, "finish_reason": "error", "message": {"content": ""}}],
        },
    ],
)
async def test_http_200_error_bodies_are_failures_not_answers(env, body):
    def transport(request):
        if request.url.path.endswith("/key"):
            return OpenRouter()(request)
        return httpx.Response(200, json=body)

    with Campaign(env / "campaign") as campaign:
        r = pinned.PinnedRouter(
            campaign,
            alias="openrouter-mimo",
            params=params(),
            cap=10,
            dispatch=True,
            transport=httpx.MockTransport(transport),
        )
        with pytest.raises(pinned.LocalFailure):
            await ask(r)
        assert kinds(campaign) == ["dispatch", "failure"]


async def test_answer_records_finish_reason(env):
    with Campaign(env / "campaign") as campaign:
        await ask(router(campaign, OpenRouter(answer=lambda _p: '{"score": 1}')))
        assert campaign.lines[-1]["finish_reason"] == "stop"


async def test_evidence_write_error_is_a_local_failure_not_a_model_error(env, monkeypatch):
    cases = {corpus.RELEVANCE: small_corpus()[corpus.RELEVANCE], RUBRIC: small_corpus()[RUBRIC]}
    directory = write_corpus(env / "corpus", cases)
    server = OpenRouter()
    with Campaign(env / "campaign") as campaign:
        original = campaign.append

        def full_disk(kind, **data):
            if kind == "answer":
                raise OSError("synthetic: no space left on device")
            return original(kind, **data)

        monkeypatch.setattr(campaign, "append", full_disk)
        report, _ = await run.qualify(
            "openrouter-mimo",
            corpus.load(directory),
            params(),
            campaign,
            env / "sqlite",
            dispatch=True,
            transport=server.transport,
        )
    for name in cases:
        for rep in report["contracts"][name]["repetitions"]:
            assert rep["errors"] == 0 and rep["unscored"] == 2 and rep["status"] == "incomplete"
    assert len(server.requests) == 1  # the router stopped sending after the fault


async def test_editing_the_corpus_mid_run_cannot_change_what_is_graded(env):
    cases = {RUBRIC: small_corpus()[RUBRIC]}
    directory = write_corpus(env / "corpus", cases)
    server = OpenRouter()
    original = server.__call__

    def edit_after_first(request):
        response = original(request)
        if len(server.requests) == 1:
            edited = {RUBRIC: [dict(c, actual=c["actual"] + " edited") for c in cases[RUBRIC]]}
            write_corpus(directory, edited)
        return response

    with Campaign(env / "campaign") as campaign:
        report, _ = await run.qualify(
            "openrouter-mimo",
            corpus.load(directory),
            params(),
            campaign,
            env / "sqlite",
            dispatch=True,
            transport=httpx.MockTransport(edit_after_first),
        )
    for rep in report["contracts"][RUBRIC]["repetitions"]:
        assert rep["errors"] == 0 and rep["unscored"] == 0 and rep["correct"] == 2


async def test_unanswered_relevance_case_is_unscored_not_a_model_error(env):
    cases = {corpus.RELEVANCE: small_corpus()[corpus.RELEVANCE]}
    directory = write_corpus(env / "corpus", cases)
    with Campaign(env / "campaign") as campaign:
        report, _ = await run.qualify(
            "openrouter-mimo",
            corpus.load(directory),
            params(),
            campaign,
            env / "sqlite",
            dispatch=False,
        )
    rep = report["contracts"][corpus.RELEVANCE]["repetitions"][0]
    assert rep["errors"] == 0 and rep["unscored"] == 2
    assert rep["unscored_reasons"] == ["unanswered"]


async def test_unreadable_secrets_file_refuses_before_any_request(env, monkeypatch):
    import dotenv

    def unreadable(_path):
        raise PermissionError("synthetic")

    monkeypatch.setattr(dotenv, "dotenv_values", unreadable)
    server = OpenRouter()
    with (
        Campaign(env / "campaign") as campaign,
        pytest.raises(pinned.LocalFailure, match="secrets.env"),
    ):
        await ask(router(campaign, server))
    assert server.requests == [] and server.key_reads == 0


async def test_a_prompt_the_router_cannot_match_is_unscored_not_a_model_error(env, monkeypatch):
    """If the map of rendered prompts ever drifts from what the scorer sends, the case
    must stay unscored: the router never served an answer for it."""
    cases = {RUBRIC: small_corpus()[RUBRIC]}
    directory = write_corpus(env / "corpus", cases)
    monkeypatch.setattr(run, "render_rubric_prompt", lambda *_a: "a different prompt")
    server = OpenRouter()
    with Campaign(env / "campaign") as campaign:
        report, _ = await run.qualify(
            "openrouter-mimo",
            corpus.load(directory),
            params(),
            campaign,
            env / "sqlite",
            dispatch=True,
            transport=server.transport,
        )
    rep = report["contracts"][RUBRIC]["repetitions"][0]
    assert rep["errors"] == 0 and rep["unscored"] == 2 and rep["status"] == "incomplete"
    assert server.requests == []


def test_a_failed_route_does_not_close_a_route_still_open(env, monkeypatch, capsys, small_floors):
    cases = {k: v for k, v in small_corpus().items() if k != corpus.NOVELTY}
    write_corpus(env / "corpus", cases)
    wrong = OpenRouter(answer=lambda _p: '{"score": 0.5, "relevance": 0.5}')
    assert cli(env, wrong, monkeypatch, "run", "--alias", "openrouter-mimo") == 2
    result = json.loads(capsys.readouterr().out)
    assert result["routes"] == {"judge": "fail", "novelty": "incomplete"}
    assert result["status"] == "incomplete"


async def test_served_cases_do_not_leak_between_contracts_sharing_case_ids(env, monkeypatch):
    first, second = (r.name for r in list_rubrics()[:2])
    cases = {
        name: [dict(c, id=f"shared-{i}") for i, c in enumerate(small_corpus()[name])]
        for name in (first, second)
    }
    directory = write_corpus(env / "corpus", cases)
    original = run.render_rubric_prompt

    def drift_second(rubric, *args):
        return "drifted" if rubric.name == second else original(rubric, *args)

    monkeypatch.setattr(run, "render_rubric_prompt", drift_second)
    with Campaign(env / "campaign") as campaign:
        report, _ = await run.qualify(
            "openrouter-mimo",
            corpus.load(directory),
            params(),
            campaign,
            env / "sqlite",
            dispatch=True,
            transport=OpenRouter().transport,
        )
    assert report["contracts"][first]["repetitions"][0]["correct"] == 2
    rep = report["contracts"][second]["repetitions"][0]
    assert rep["errors"] == 0 and rep["unscored"] == 2
