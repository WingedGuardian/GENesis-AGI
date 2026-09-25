"""A RESUMED CC turn's reported cost is session-cumulative from CC 2.1.277.

MEASURED on CC 2.1.280 (three `claude -p --model haiku` calls in one resumed
session): `total_cost_usd` 0.0378 -> 0.0415 -> 0.0450 and `modelUsage` output
tokens 101 -> 134 -> 167 are running totals, while `usage.output_tokens`
101 -> 33 -> 33 stays per call. The foreground conversation path resumes every
turn after the first and used to ADD the reported cost, so each turn re-added
the whole session. The payloads below are those three real results, trimmed to
the fields the parser reads.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation, CCModel
from genesis.db.crud import cc_sessions

_REPO = Path(__file__).resolve().parents[2]

_MODEL = "claude-haiku-4-5-20251001"
# (total_cost_usd, usage.output_tokens, modelUsage outputTokens) per real call.
_PROBE = [(0.0378199, 101, 101), (0.0414546, 33, 134), (0.044968, 33, 167)]


def _result(total, out_call, out_model, sid="cc-A"):
    return {
        "type": "result",
        "session_id": sid,
        "result": "ok",
        "total_cost_usd": total,
        "usage": {"input_tokens": 10, "output_tokens": out_call},
        "modelUsage": {_MODEL: {"inputTokens": 10, "outputTokens": out_model, "costUSD": total}},
    }


def _parse(result):
    inv = CCInvocation(prompt="x", model=CCModel.HAIKU)
    return CCInvoker()._parse_result_dict(result, inv, 1000)


# ── detection, at the one parse chokepoint ──────────────────────────────


def test_cumulative_is_detected_from_the_output_itself():
    flags = [_parse(_result(*p)).cost_is_cumulative for p in _PROBE]
    assert flags == [False, True, True]


def test_pre_2_1_277_per_call_shape_is_not_cumulative():
    """Before 2.1.277 a resumed call's totals restarted at zero, so its model
    totals equal its own tokens — nothing to subtract."""
    assert _parse(_result(0.004, 33, 33)).cost_is_cumulative is False


def test_missing_model_usage_is_not_cumulative():
    r = _result(0.01, 5, 5)
    del r["modelUsage"]
    assert _parse(r).cost_is_cumulative is False


# ── recording ───────────────────────────────────────────────────────────


@pytest.fixture
async def row(db):
    await cc_sessions.create(
        db,
        id="s1",
        session_type="foreground",
        model="haiku",
        effort="medium",
        status="active",
        user_id="u",
        channel="telegram",
        started_at="2026-09-25T00:00:00",
        last_activity_at="2026-09-25T00:00:00",
    )
    return "s1"


async def _record(db, row_id, output):
    await cc_sessions.record_turn_cost(
        db,
        row_id,
        cc_session_id=output.session_id,
        reported_cost_usd=output.cost_usd,
        cumulative=output.cost_is_cumulative,
        input_tokens=output.input_tokens,
        output_tokens=output.output_tokens,
    )


async def test_three_resumed_turns_record_the_session_total_not_its_running_sum(db, row):
    for p in _PROBE:
        await _record(db, row, _parse(_result(*p)))
    r = await cc_sessions.get_by_id(db, row)
    # The old ADD path stored 0.0378+0.0415+0.0450 = 0.1242 — 2.8x the truth.
    assert r["cost_usd"] == pytest.approx(0.044968)
    assert r["output_tokens"] == 101 + 33 + 33  # tokens were always per call


async def test_pre_2_1_277_resumed_turns_still_add(db, row):
    for total in (0.03, 0.004, 0.005):
        await _record(db, row, _parse(_result(total, 10, 10)))
    r = await cc_sessions.get_by_id(db, row)
    assert r["cost_usd"] == pytest.approx(0.039)


async def test_a_fresh_cc_session_on_the_same_row_restarts_the_cursor(db, row):
    """_reconstruct_resume can degrade to a FRESH CC session on the same row.
    That session's cumulative total starts again from its own first call; it
    must not be diffed against the previous session's total."""
    await _record(db, row, _parse(_result(0.03, 10, 10, sid="cc-A")))
    await _record(db, row, _parse(_result(0.05, 10, 20, sid="cc-A")))  # +0.02
    await _record(db, row, _parse(_result(0.01, 10, 10, sid="cc-B")))  # fresh: +0.01
    await _record(db, row, _parse(_result(0.018, 10, 20, sid="cc-B")))  # +0.008
    r = await cc_sessions.get_by_id(db, row)
    assert r["cost_usd"] == pytest.approx(0.05 + 0.018)


async def test_a_cumulative_report_from_another_cc_session_is_not_diffed_against_it(db, row):
    """The cursor still names CC session A when B's first recorded report is
    already cumulative (its first turn was lost, or ran elsewhere). Diffing B
    against A's larger total would clamp B's turn to zero."""
    await _record(db, row, _parse(_result(0.05, 10, 10, sid="cc-A")))
    await _record(db, row, _parse(_result(0.02, 10, 20, sid="cc-B")))
    r = await cc_sessions.get_by_id(db, row)
    assert r["cost_usd"] == pytest.approx(0.05 + 0.02)


async def test_a_report_below_its_cursor_is_added_whole_not_clamped(db, row):
    """A true running total never DROPS within one CC session, so a 'cumulative'
    report below the cursor was misread — record it, never zero a real turn."""
    await _record(db, row, _parse(_result(0.05, 10, 10)))
    await _record(db, row, _parse(_result(0.02, 10, 20)))  # flagged, but went down
    r = await cc_sessions.get_by_id(db, row)
    assert r["cost_usd"] == pytest.approx(0.07)


def test_subagent_tokens_on_another_model_do_not_look_cumulative():
    """Old-CC shape with a subagent on a different model: the main model's own
    entry equals this call's tokens, so it is per call, whatever else is listed."""
    r = {
        "type": "result",
        "session_id": "cc-A",
        "result": "ok",
        "total_cost_usd": 0.02,
        "usage": {"input_tokens": 10, "output_tokens": 101},
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"outputTokens": 400},  # the subagent
            "claude-sonnet-5": {"outputTokens": 101},  # the main session
        },
    }
    out = _parse(r)
    assert out.model_used == "claude-sonnet-5"  # guard: main model resolved as intended
    assert out.cost_is_cumulative is False


@pytest.mark.parametrize("model_usage", [None, "garbage", {_MODEL: {"outputTokens": "x"}}])
def test_odd_model_usage_never_breaks_the_parse_point(model_usage):
    r = _result(0.01, 5, 5)
    r["modelUsage"] = model_usage
    assert _parse(r).cost_is_cumulative is False


async def test_a_cumulative_report_seen_without_a_cursor_is_added_whole(db, row):
    """No cursor for this CC session (e.g. the first recorded turn was lost) —
    the only safe reading is the reported total itself; nothing to subtract."""
    await _record(db, row, _parse(_result(0.05, 10, 20, sid="cc-A")))
    r = await cc_sessions.get_by_id(db, row)
    assert r["cost_usd"] == pytest.approx(0.05)


async def test_corrupt_metadata_is_tolerated_and_kept_recoverable(db, row):
    await db.execute("UPDATE cc_sessions SET metadata = 'not json' WHERE id = ?", (row,))
    await db.commit()
    await _record(db, row, _parse(_result(0.03, 10, 10)))
    await _record(db, row, _parse(_result(0.05, 10, 20)))
    r = await cc_sessions.get_by_id(db, row)
    assert r["cost_usd"] == pytest.approx(0.05)
    assert json.loads(r["metadata"])["cost_cursor"] == {"cc_session_id": "cc-A", "total": 0.05}


async def test_other_metadata_keys_survive(db, row):
    await cc_sessions.merge_metadata(db, row, {"roster_model": "claude"})
    await _record(db, row, _parse(_result(0.03, 10, 10)))
    assert (
        json.loads((await cc_sessions.get_by_id(db, row))["metadata"])["roster_model"] == "claude"
    )


# ── wiring ──────────────────────────────────────────────────────────────


def test_the_foreground_turn_paths_record_through_the_cumulative_aware_writer():
    """Both foreground turn paths (non-streaming and streaming) resume, so both
    must use record_turn_cost. A bare increment_cost there re-adds the session."""
    tree = ast.parse((_REPO / "src/genesis/cc/conversation.py").read_text())
    called = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"increment_cost", "record_turn_cost"}
    ]
    assert called.count("record_turn_cost") == 2, called
    assert "increment_cost" not in called, called
