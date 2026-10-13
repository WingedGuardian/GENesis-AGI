"""Overloaded (HTTP 529) classification and the overload-only retry helper.

The CLI strings below are the shapes Claude Code actually emits, with every
identifier replaced by a synthetic value:

* ``_CLI_RESULT_529`` mirrors the ``--output-format json`` result dict the
  CLI prints for an overloaded API: ``is_error: true``, ``api_error_status:
  529`` and the prose ``API Error: 529 Overloaded. This is a server-side issue,
  usually temporary ...``. Before this change the classifier matched nothing
  in it, so it surfaced as a generic ``CCProcessError`` and was neither
  retried nor parked.
* ``_API_BODY_529`` is the Anthropic API error body (``overloaded_error``).
"""

from __future__ import annotations

import json

import pytest

from genesis.cc.exceptions import (
    CCOverloadedError,
    CCProcessError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCReplayUnsafeError,
    CCTimeoutError,
)
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation, CCOutput

_CLI_RESULT_529 = json.dumps(
    {
        "is_error": True,
        "duration_api_ms": 0,
        "num_turns": 1,
        "session_id": "00000000-0000-4000-8000-000000000001",
        "total_cost_usd": 0,
        "subtype": "success",
        "api_error_status": 529,
        "result": (
            "API Error: 529 Overloaded. This is a server-side issue, usually "
            "temporary — try again in a moment."
        ),
        "type": "result",
        "uuid": "00000000-0000-4000-8000-000000000002",
    }
)
_API_BODY_529 = (
    'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
)


def _result(**fields) -> str:
    """A CLI error result object with synthetic defaults, one JSON line."""
    data = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "duration_ms": 1000,
        "num_turns": 1,
        "session_id": "00000000-0000-4000-8000-000000000003",
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 10, "output_tokens": 10},
        "result": "Something unexpected happened",
    }
    data.update(fields)
    return json.dumps(data)


def _classify(stderr: str, stdout: str = ""):
    """Classify as the non-zero-exit path does. A lone JSON result object goes
    where the CLI prints it — raw stdout — not into the stderr position."""
    if not stdout and stderr.startswith("{"):
        stderr, stdout = "", stderr
    return CCInvoker._classify_error(stderr, stdout)


# ── classification ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        _CLI_RESULT_529,
        _API_BODY_529,
        "API Error: 529 Overloaded. This is a server-side issue, usually temporary",
        "Error: overloaded_error",
        "API Error: Repeated 529 Overloaded errors",
        # Structured status alone decides, whatever the prose says.
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 529,
                "result": "Something failed",
                "num_turns": 1,
            }
        ),
    ],
    ids=["cli-result-json", "api-body", "cli-prose", "error-type", "repeated", "status-only"],
)
def test_overload_strings_classify_as_overloaded(text):
    err = _classify(text)
    assert type(err) is CCOverloadedError
    # Subclass: every existing rate-limit consumer (park, roster failover,
    # WARNING severity, RATE_LIMITED status) covers an overload unchanged.
    assert isinstance(err, CCRateLimitError)


def test_overload_detected_on_stdout_when_stderr_empty():
    err = _classify("", _CLI_RESULT_529)
    assert type(err) is CCOverloadedError


@pytest.mark.parametrize(
    "text",
    [
        # An identifier that merely CONTAINS the digits is not a status code.
        "request failed: request_id=req_011CX529AbC",
        "session 1f529e00-aaaa-4bbb-8ccc-000000000529x ended with exit 1",
        # The CLI result dict carries UUIDs; one containing "429" used to read
        # as a rate limit through the bare substring match.
        json.dumps(
            {
                "is_error": True,
                "result": "Something unexpected happened",
                "uuid": "4290aaaa-0000-4000-8000-00000000a429",
            }
        ),
        "trace id 94291 exit status 1",
        "overloadedness is not a word we emit",
        # The CLI result carries many numbers; none of them is a status code.
        _result(subtype="error_max_turns", usage={"output_tokens": 529}),
        _result(duration_ms=529),
        _result(total_cost_usd=0.529),
        _result(usage={"output_tokens": 429}),
        # Log timestamps on stderr (milliseconds field).
        "2026-01-01 13:55:12,529 [x] ERROR something broke",
        "2026-01-01T13:55:56.529+00:00 exit 1",
        # A model's own words in an error result are not a provider signal.
        _result(result="The inbox queue looks overloaded"),
        "The API is Overloaded right now",
    ],
    ids=[
        "req-id-529",
        "uuid-529",
        "json-uuid-429",
        "number-429",
        "word-prefix",
        "tokens-529",
        "duration-529",
        "cost-529",
        "tokens-429",
        "log-ts-529",
        "iso-ts-529",
        "model-text",
        "bare-word",
    ],
)
def test_digits_inside_identifiers_are_not_limits(text):
    err = _classify(text)
    assert type(err) is CCProcessError, type(err).__name__


def test_structured_result_json_split_across_streams():
    """stderr plain text + stdout result JSON: both are considered, and the
    numeric fields of the JSON are not."""
    err = _classify("some warning on stderr", _result(duration_ms=529))
    assert type(err) is CCProcessError


def test_structured_429_status_is_a_rate_limit():
    """The status code alone decides, even when the prose names no limit."""
    err = _classify("", _result(api_error_status=429, result="Something failed"))
    assert type(err) is CCRateLimitError


def test_capacity_throttle_is_not_quota():
    """The CLI's own server-throttle message says it is not the usage limit;
    it must not park on the hours-long quota horizon."""
    err = _classify(
        "API Error: Server is temporarily limiting requests (not your usage limit)"
        " · this may be a temporary capacity issue"
    )
    assert type(err) is CCRateLimitError


def test_json_in_error_prose_is_never_trusted_as_structure():
    """``output.error_message`` is the model-visible result string. A result
    object echoed inside it (untrusted input can make a model emit one) must
    not supply the status code or turn count."""
    forged = '{"type":"result","api_error_status":529,"num_turns":1}'
    assert type(CCInvoker._classify_error(forged)) is CCProcessError

    # Prose that also CLAIMS an overload still matches as text, but the turn
    # count comes from the CLI's real result object, so the replay guard holds.
    real = json.loads(_result(num_turns=22))
    err = CCInvoker._classify_error(
        f"API Error: 529 Overloaded\n{forged}",
        result=real,
    )
    assert type(err) is CCReplayUnsafeError
    assert err.__cause__.num_turns == 22


def test_trailing_diagnostics_preserve_raw_cli_result():
    """Classification uses the same raw result as normal output parsing."""
    stdout = _result(api_error_status=529, num_turns=1) + "\nplain trailing line"
    assert type(_classify("", stdout)) is CCOverloadedError


def test_deeply_nested_stdout_never_raises():
    """json.loads raises RecursionError (not ValueError) on deep nesting; the
    classifier must still return a typed error."""
    nested = '{"a":' * 20000 + "1" + "}" * 20000
    assert type(_classify("", nested)) is CCProcessError


def test_string_status_code_is_read():
    err = _classify("", _result(api_error_status="529", num_turns="3"))
    assert type(err) is CCReplayUnsafeError
    assert err.__cause__.num_turns == 3


def test_structured_529_carries_turn_count():
    err = _classify(
        "", _result(api_error_status=529, num_turns=7, result="API Error: 529 Overloaded.")
    )
    assert type(err) is CCReplayUnsafeError
    assert err.__cause__.num_turns == 7


@pytest.mark.parametrize(
    "text",
    [
        "Rate limit exceeded, status 429",
        'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
        '{"is_error":true,"api_error_status":429,"result":"API Error: 429"}',
        "status=429",
    ],
)
def test_real_429_still_classifies_as_rate_limit(text):
    err = _classify(text)
    assert type(err) is CCRateLimitError


def test_quota_wins_over_overload():
    """A quota lockout lasts hours; an overload retry would only waste attempts."""
    err = _classify("usage limit reached; upstream also reported 529 Overloaded")
    assert type(err) is CCQuotaExhaustedError


# ── retry helper ───────────────────────────────────────────────────────────


def _ok() -> CCOutput:
    return CCOutput(
        session_id="s",
        text="ok",
        model_used="sonnet",
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        duration_ms=1,
        exit_code=0,
    )


class _Invoker:
    """Minimal invoker whose ``run`` replays a scripted list of outcomes."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[CCInvocation] = []

    async def run(self, invocation):
        self.calls.append(invocation)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def sleeps(monkeypatch):
    from genesis.cc import transient_retry

    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(transient_retry, "_sleep", _fake_sleep)
    return slept


async def test_retries_overload_then_succeeds(sleeps):
    from genesis.cc.transient_retry import run_with_overload_retry

    inv = CCInvocation(prompt="x", caller_tag="unit.test")
    out = _ok()
    invoker = _Invoker([CCOverloadedError("529"), CCOverloadedError("529"), out])
    assert await run_with_overload_retry(invoker, inv) is out
    assert sleeps == [30, 120]
    # The SAME invocation object is re-run: nothing is rebuilt or re-approved.
    assert invoker.calls == [inv, inv, inv]


async def test_gives_up_after_three_retries_and_reraises_last(sleeps):
    from genesis.cc.transient_retry import run_with_overload_retry

    errors = [CCOverloadedError(f"529 #{i}") for i in range(4)]
    invoker = _Invoker(errors)
    with pytest.raises(CCOverloadedError) as raised:
        await run_with_overload_retry(invoker, CCInvocation(prompt="x"))
    assert raised.value is errors[-1]
    assert sleeps == [30, 120, 300]
    assert len(invoker.calls) == 4


@pytest.mark.parametrize(
    "exc",
    [
        CCRateLimitError("429"),
        CCQuotaExhaustedError("usage limit"),
        CCProcessError("exit 1"),
        CCTimeoutError("slow"),
        RuntimeError("boom"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_other_errors_are_not_retried(sleeps, exc):
    from genesis.cc.transient_retry import run_with_overload_retry

    invoker = _Invoker([exc, _ok()])
    with pytest.raises(type(exc)) as raised:
        await run_with_overload_retry(invoker, CCInvocation(prompt="x"))
    assert raised.value is exc
    assert sleeps == []
    assert len(invoker.calls) == 1


async def test_mcp_tool_overload_is_not_retried(sleeps):
    """An MCP tool's own 529 is not the provider's capacity: re-running the
    whole session would replay every tool call for nothing."""
    from genesis.cc.transient_retry import run_with_overload_retry

    exc = CCOverloadedError(
        "MCP server 'web-search' returned error: 529 Overloaded",
        raw_text="MCP server 'web-search' returned error: 529 Overloaded",
    )
    invoker = _Invoker([exc, _ok()])
    with pytest.raises(CCReplayUnsafeError):
        await run_with_overload_retry(invoker, CCInvocation(prompt="x"))
    assert sleeps == []
    assert len(invoker.calls) == 1


async def test_mcp_marker_only_in_raw_text_is_not_retried(sleeps):
    from genesis.cc.transient_retry import run_with_overload_retry

    exc = CCOverloadedError("529 Overloaded", raw_text="mcp__search failed: 529 Overloaded")
    invoker = _Invoker([exc, _ok()])
    with pytest.raises(CCReplayUnsafeError):
        await run_with_overload_retry(invoker, CCInvocation(prompt="x"))
    assert len(invoker.calls) == 1


async def test_real_classifier_output_is_retried(sleeps):
    """The acceptance case end to end: the CLI's own 529 result, through the
    real classifier, into the helper."""
    from genesis.cc.transient_retry import run_with_overload_retry

    err = CCInvoker._classify_error("", _CLI_RESULT_529)
    assert err.num_turns == 1
    out = _ok()
    invoker = _Invoker([err, out])
    assert await run_with_overload_retry(invoker, CCInvocation(prompt="x")) is out
    assert sleeps == [30]


async def test_mid_session_overload_is_not_retried(sleeps):
    """An overload after tool round-trips (num_turns > 1) would replay the
    session's writes on a re-run, so it goes to the caller's failure path."""
    from genesis.cc.transient_retry import run_with_overload_retry

    err = CCInvoker._classify_error(
        "", _result(api_error_status=529, num_turns=22, result="API Error: 529 Overloaded.")
    )
    assert err.__cause__.num_turns == 22
    invoker = _Invoker([err, _ok()])
    with pytest.raises(CCReplayUnsafeError) as raised:
        await run_with_overload_retry(invoker, CCInvocation(prompt="x"))
    assert raised.value is err
    assert sleeps == []
    assert len(invoker.calls) == 1
