"""An executor step's status uses the session's own final report (cc/final_status.py).

A step denied what it needed usually exits cleanly and claims nothing, so
``parse_step_output`` defaulted it to completed. Now a reported ``blocked`` or
``failed`` wins over that default; the engine's existing blocker notification
then reaches the owner. A reported ``failed`` that contradicts an explicit JSON
``completed`` is ``blocked``, never ``failed``: failed starts automatic re-runs
of a step that may already have done its work.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor.step_dispatcher import StepDispatcher, _step_status
from genesis.cc.final_status import FinalStatus
from genesis.cc.types import CCOutput


def _out(text: str = "did the thing", report=None) -> CCOutput:
    return CCOutput(
        session_id="cc-1",
        text=text,
        model_used="sonnet",
        cost_usd=0.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=10,
        exit_code=0,
        final_status=report,
    )


DENIED = FinalStatus("blocked", "secrets.env denied")


@pytest.mark.parametrize(
    ("parsed", "report", "expected"),
    [
        # No JSON at all: the report decides a stop, and its reason is the result.
        ({}, DENIED, ("blocked", "secrets.env denied", "secrets.env denied")),
        ({}, FinalStatus("failed", "tests fail"), ("failed", "tests fail", "tests fail")),
        ({}, FinalStatus("blocked"), ("blocked", "Step reported blocked", "Step reported blocked")),
        # Nothing to stop on: the old default, untouched.
        ({}, FinalStatus("completed", "done"), ("completed", None, None)),
        ({}, None, ("completed", None, None)),
        ({}, FinalStatus("malformed"), ("completed", None, None)),
        # An explicit JSON "completed": a reported block wins; a reported failure
        # becomes a block (the owner decides, nothing re-runs automatically).
        ({"status": "completed"}, DENIED, ("blocked", "secrets.env denied", "secrets.env denied")),
        ({"status": "completed"}, FinalStatus("failed", "x"), ("blocked", "x", "x")),
        # The step's own result text is kept when it gave one.
        ({"status": "completed", "result": "r"}, DENIED, ("blocked", "secrets.env denied", None)),
        # Any other JSON status stands, with its own blocker text.
        (
            {"status": "blocked", "blocker_description": "own words"},
            FinalStatus("failed", "x"),
            ("blocked", "own words", None),
        ),
        ({"status": "failed"}, FinalStatus("completed"), ("failed", None, None)),
    ],
)
def test_the_step_status_table(parsed, report, expected):
    assert _step_status(parsed, _out(report=report)) == expected


def test_an_output_without_the_field_keeps_the_old_default():
    class Legacy:
        text = "x"

    assert _step_status({}, Legacy()) == ("completed", None, None)


async def test_a_step_asks_for_a_report_and_a_reported_block_surfaces():
    invoker = AsyncMock()
    invoker.run = AsyncMock(
        return_value=_out(text=json.dumps({"result": "partial"}), report=DENIED)
    )
    sd = StepDispatcher(db=AsyncMock(), invoker=invoker, autonomous_dispatcher=None)
    step = {"idx": 0, "type": "code", "description": "test step"}
    result = await sd.dispatch_step("t-1", step, [])
    assert invoker.run.await_args.args[0].report_status is True
    assert (result.status, result.blocker_description, result.result) == (
        "blocked",
        "secrets.env denied",
        "partial",
    )
