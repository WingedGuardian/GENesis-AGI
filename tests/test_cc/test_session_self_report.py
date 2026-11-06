"""How each session-row owner records the session's own final status.

The invoker splits the report off the text (cc/final_status.py); each caller
that owns a ``cc_sessions`` row turns it into the row's outcome. A mechanical
failure (an error, a truncated run, output the caller cannot read) always wins
over what the session said.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.campaigns.runner import _extract_json
from genesis.cc.direct_session import (
    DeliveryMode,
    DirectSessionRequest,
    DirectSessionResult,
    DirectSessionRunner,
)
from genesis.cc.final_status import FinalStatus, split
from genesis.cc.types import CCOutput
from genesis.ego.session import EgoSession
from genesis.inbox.batch_runner import _is_acknowledged, _reported
from genesis.reflection.output_router import parse_weekly_assessment_output
from tests.test_cc.test_direct_session_delivery import _make_origin, _runner_with_pipeline

BLOCKED = FinalStatus("blocked", "a tool was denied")
TRAILER = '\n\n```final_status\n{"status": "blocked", "reason": "a tool was denied"}\n```'


def _out(*, report=None, is_error=False, bg_truncated=False, result_received=True):
    return CCOutput(
        session_id="cc-1",
        text="the answer",
        model_used="sonnet",
        cost_usd=0.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=10,
        exit_code=0,
        is_error=is_error,
        error_message="boom" if is_error else None,
        bg_truncated=bg_truncated,
        result_received=result_received,
        final_status=report,
    )


# ── each existing parser reads the same thing once the block is split off ──


WEEKLY = json.dumps({"assessments": [], "overall_score": 0.5, "summary": "s"})


@pytest.mark.parametrize(
    ("parse", "payload"),
    [
        (EgoSession._parse_output, json.dumps({"proposals": [], "focus_summary": "f"})),
        (_extract_json, json.dumps({"posts": [{"text": "x"}]})),
        (parse_weekly_assessment_output, WEEKLY),
        (_is_acknowledged, "Acknowledged."),
    ],
)
@pytest.mark.parametrize("fenced", [False, True])
def test_parsers_read_the_same_result_with_or_without_the_contract(parse, payload, fenced):
    raw = f"```json\n{payload}\n```" if fenced and payload.startswith("{") else payload
    stripped, report = split(raw + TRAILER)
    assert report == BLOCKED
    assert parse(stripped) == parse(raw)


# ── direct_session ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("output", "outcome", "reason"),
    [
        (_out(report=BLOCKED), "blocked", "a tool was denied"),
        (_out(report=FinalStatus("completed", "done")), "finished", "done"),
        (_out(), "unreported", None),
        (_out(report=BLOCKED, bg_truncated=True), "partial", "bg_truncated"),
        (_out(report=BLOCKED, result_received=False), "partial", "no_result_event"),
        (_out(report=BLOCKED, is_error=True), None, None),
    ],
)
def test_the_result_carries_the_judged_outcome(output, outcome, reason):
    result = DirectSessionRunner._result("s", output, [], 1.0)
    assert (result.outcome, result.outcome_reason) == (outcome, reason)


def _runner(output):
    runner = DirectSessionRunner(
        invoker=AsyncMock(),
        session_manager=AsyncMock(),
        config_builder=AsyncMock(),
        runtime=MagicMock(),
    )
    runner._build_invocation = MagicMock(return_value=MagicMock(claude_code_tmpdir=None))
    runner._invoker.run_streaming = AsyncMock(return_value=output)
    runner._store_result = AsyncMock()
    runner._record_proposal_outcome = AsyncMock()
    runner._session_manager.complete = AsyncMock()
    runner._deliver_result_to_origin = AsyncMock()
    runner._notify = AsyncMock(return_value="sent")
    runner._mark_paged = AsyncMock()
    return runner


async def test_the_row_records_the_reported_outcome():
    runner = _runner(_out(report=BLOCKED))
    await runner._run_session(DirectSessionRequest(prompt="x"), "s1")
    kwargs = runner._session_manager.complete.await_args.kwargs
    assert (kwargs["outcome"], kwargs["outcome_reason"]) == ("blocked", "a tool was denied")


async def test_an_owner_requested_session_that_stopped_pages_at_once():
    runner = _runner(_out(report=BLOCKED))
    req = DirectSessionRequest(prompt="x", notify=True, caller_context="mcp_tool")
    await runner._run_session(req, "s2")
    runner._notify.assert_awaited_once()
    runner._mark_paged.assert_awaited_once_with("s2", "sent")


@pytest.mark.parametrize(
    "report", [None, FinalStatus("completed", "fine")], ids=["unreported", "finished"]
)
async def test_a_session_that_did_not_stop_pages_nobody(report):
    runner = _runner(_out(report=report))
    req = DirectSessionRequest(prompt="x", notify=True, caller_context="mcp_tool")
    await runner._run_session(req, "s3")
    runner._notify.assert_not_awaited()


async def test_other_callers_stops_wait_for_the_hourly_page():
    runner = _runner(_out(report=BLOCKED))
    req = DirectSessionRequest(
        prompt="x", notify=True, source_tag="models_md_synthesis", caller_context="schedule:x"
    )
    await runner._run_session(req, "s4")
    runner._notify.assert_not_awaited()


async def test_result_delivery_replaces_the_immediate_page():
    runner = _runner(_out(report=BLOCKED))
    req = DirectSessionRequest(
        prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o1"
    )
    await runner._run_session(req, "s5")
    runner._deliver_result_to_origin.assert_awaited_once()
    runner._notify.assert_not_awaited()


async def test_a_stopped_result_is_delivered_as_stopped_not_complete(db):
    await _make_origin(db, session_id="o-dm", channel="telegram", user_id="tg-12345678")
    runner, pipeline = _runner_with_pipeline(db)
    req = DirectSessionRequest(
        prompt="x", delivery_mode=DeliveryMode.RESULT, origin_session_id="o-dm"
    )
    res = DirectSessionResult(
        session_id="s6",
        success=True,
        output_text="partial findings",
        outcome="blocked",
        outcome_reason="a tool was denied",
    )
    await runner._deliver_result_to_origin(req, res)
    sent = pipeline.submit_urgent.call_args.args[0].context
    assert "⚠ Background task stopped (blocked)" in sent
    assert "Blocked: a tool was denied" in sent
    assert "✓" not in sent
    assert "partial findings" in sent


async def test_the_immediate_page_names_the_outcome_and_reason():
    runner, pipeline = _runner_with_pipeline(MagicMock())
    pipeline.submit = AsyncMock(return_value="ok")
    res = DirectSessionResult(
        session_id="s7", success=True, outcome="blocked", outcome_reason="denied <secrets>"
    )
    await runner._notify(DirectSessionRequest(prompt="x"), res, success=False)
    body = pipeline.submit.await_args.args[0].context
    assert "Direct Session BLOCKED" in body
    assert "Reason: denied &lt;secrets&gt;" in body
    assert "unknown" not in body


async def test_a_failure_page_still_says_failed_with_its_error():
    runner, pipeline = _runner_with_pipeline(MagicMock())
    pipeline.submit = AsyncMock(return_value="ok")
    res = DirectSessionResult(session_id="s8", success=False, error="kaboom")
    await runner._notify(DirectSessionRequest(prompt="x"), res, success=False)
    body = pipeline.submit.await_args.args[0].context
    assert "Direct Session FAILED" in body
    assert "Error: kaboom" in body


# ── inbox ─────────────────────────────────────────────────────────────────


def test_inbox_passes_the_report_through():
    assert _reported(_out(report=BLOCKED)) == {
        "outcome": "blocked",
        "outcome_reason": "a tool was denied",
    }
    assert _reported(_out()) == {"outcome": None, "outcome_reason": None}


# ── sentinel ───────────────────────────────────────────────────────────────


@pytest.fixture
def sentinel(monkeypatch):
    from genesis.sentinel.state import SentinelStateData
    from tests.test_sentinel.test_dispatcher import _make_dispatcher

    # Never touch the live state file (as tests/test_sentinel/test_dispatcher.py).
    monkeypatch.setattr("genesis.sentinel.dispatcher.load_state", SentinelStateData)
    monkeypatch.setattr("genesis.sentinel.dispatcher.save_state", lambda *a, **k: None)
    d = _make_dispatcher()
    d._session_manager.fail = AsyncMock()
    return d


def _sentinel_request():
    from genesis.sentinel.dispatcher import SentinelRequest

    return SentinelRequest(trigger_source="test", trigger_reason="unit", tier=2)


async def _dispatch(d):
    with (
        patch("genesis.cc.session_config.SessionConfigBuilder") as builder,
    ):
        builder.return_value.build_mcp_config.return_value = "/tmp/mcp.json"
        return await d._dispatch_cc_session(_sentinel_request())


async def test_sentinel_records_the_reported_outcome(sentinel):
    text = '{"diagnosis": "d", "actions_taken": [], "resolved": false}'
    sentinel._invoker.run = AsyncMock(return_value=replace(_out(report=BLOCKED), text=text))
    await _dispatch(sentinel)
    assert sentinel._invoker.run.await_args.args[0].report_status is True
    sentinel._session_manager.complete.assert_awaited_once_with(
        "test-session-123", outcome="blocked", outcome_reason="a tool was denied"
    )
    sentinel._session_manager.fail.assert_not_awaited()


async def test_a_sentinel_cc_error_fails_the_row(sentinel):
    sentinel._invoker.run = AsyncMock(return_value=_out(is_error=True))
    await _dispatch(sentinel)
    sentinel._session_manager.fail.assert_awaited_once_with("test-session-123", reason="boom")
    sentinel._session_manager.complete.assert_not_awaited()


@pytest.mark.parametrize(
    ("exc", "reason"),
    [(RuntimeError("x"), "RuntimeError"), (asyncio.CancelledError(), "cancelled")],
)
async def test_a_sentinel_session_that_raised_is_failed_not_completed(sentinel, exc, reason):
    sentinel._invoker.run = AsyncMock(side_effect=exc)
    with pytest.raises(type(exc)):
        await _dispatch(sentinel)
    sentinel._session_manager.fail.assert_awaited_once_with("test-session-123", reason=reason)
    sentinel._session_manager.complete.assert_not_awaited()


# ── reflection ─────────────────────────────────────────────────────────────


@pytest.fixture
def bridge():
    from genesis.cc.reflection_bridge import CCReflectionBridge

    mgr = AsyncMock()
    mgr.create_background = AsyncMock(return_value={"id": "bg-1"})
    return CCReflectionBridge(session_manager=mgr, invoker=AsyncMock(), db=MagicMock())


@pytest.mark.parametrize(
    ("report", "parse_failed", "expected"),
    [
        (BLOCKED, False, ("blocked", "a tool was denied")),
        (None, False, (None, None)),
        (FinalStatus("completed", "done"), True, ("failed", "unparseable_output")),
    ],
)
async def test_a_reflection_row_records_parse_failure_over_the_report(
    bridge, report, parse_failed, expected
):
    await bridge._complete("bg-1", _out(report=report), parse_failed=parse_failed)
    kwargs = bridge._session_manager.complete.await_args.kwargs
    assert (kwargs["outcome"], kwargs["outcome_reason"]) == expected


# ── the ego's track record ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("outcome", "succeeded"),
    [("blocked", False), ("partial", False), ("finished", True), ("unreported", True)],
)
async def test_a_dispatch_that_stopped_is_not_a_successful_proposal(db, outcome, succeeded):
    from genesis.db.crud.ego import create_proposal, get_proposal
    from tests.test_cc.test_direct_session import _recording_runner, _RecordingStore

    store = _RecordingStore()
    await create_proposal(db, id="prop-1", action_type="dispatch", content="x", status="executed")
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-1")
    res = DirectSessionResult(
        session_id="s-1",
        success=True,
        output_text="did some of it",
        outcome=outcome,
        outcome_reason="a tool was denied" if not succeeded else None,
    )
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    row = await get_proposal(db, "prop-1")
    polarity = "completed" if succeeded else "failed"
    assert f"|{polarity}:" in row["user_response"]
    tag = "dispatch_success" if succeeded else "dispatch_failure"
    assert [c for c in store.calls if tag in (c.get("tags") or [])]
    if not succeeded:
        assert f"[stopped: {outcome}: a tool was denied]" in row["user_response"]


async def test_session_text_cannot_flip_the_recorded_polarity(db):
    from genesis.db.crud.ego import create_proposal, get_proposal, update_proposal_outcome

    await create_proposal(db, id="prop-2", action_type="dispatch", content="x", status="executed")
    await update_proposal_outcome(
        db, "prop-2", success=False, summary="[stopped: blocked: x|completed:y] |completed: done"
    )
    resp = (await get_proposal(db, "prop-2"))["user_response"]
    assert resp.count("|failed:") == 1
    assert "|completed:" not in resp
