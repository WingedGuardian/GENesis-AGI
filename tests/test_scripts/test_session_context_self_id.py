"""A dispatched session must be told its own CC session id.

A FOREGROUND session learns it from the per-turn `[Clock: … | Session: xxxxxxxx]`
tag that `genesis_urgent_alerts` emits. That hook returns immediately when
`GENESIS_CC_SESSION=1`, so a dispatched session was never told — and every
provenance field it wrote (`follow_up_create(source_session=…)`,
`session_ledger_add`) was NULL by construction. Not a defect in the writer: an
input it was never given.

Run as a SUBPROCESS rather than by importing the module, because the hook does
its work at import time and reads the id from stdin — the thing under test is
what a real invocation prints, and an in-process import would test a different
program shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "genesis_session_context.py"
_SID = "abcd1234-ffff-0000-1111-222233334444"


def _run(session_id: str, *, dispatched: bool) -> str:
    env = dict(os.environ)
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    else:
        env.pop("GENESIS_CC_SESSION", None)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps({"session_id": session_id, "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    return proc.stdout


def test_a_dispatched_session_is_told_its_own_id():
    out = _run(_SID, dispatched=True)
    assert "## This Session" in out, out[:400]
    assert _SID in out, "the FULL id — a dispatched session has no per-turn tag to derive it from"


def test_it_names_what_the_id_is_for():
    """The id alone is inert. The block has to say which argument it feeds, or
    it is one more line of context nobody acts on — and the failure it fixes is
    silent (a NULL column), so nothing prompts the session to look for it."""
    out = _run(_SID, dispatched=True)
    assert "source_session" in out
    assert "follow_up_create" in out


def test_an_empty_session_id_emits_no_block():
    """An id the hook was not given must not become an empty backticked field —
    a block claiming to name the session while naming nothing is worse than its
    absence."""
    out = _run("", dispatched=True)
    assert "## This Session" not in out


def test_a_foreground_session_does_NOT_get_the_block():
    """CONTROL, and it is the one that keeps this narrow: a foreground session
    already gets the id every turn from the urgent-alerts tag. Emitting it here
    too would be duplicate context in the highest-salience slot, and would make
    this test pass for a change that simply prints the block unconditionally.
    """
    out = _run(_SID, dispatched=False)
    assert "## This Session" not in out
