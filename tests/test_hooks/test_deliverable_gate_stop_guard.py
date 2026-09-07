"""The deliverable gate must not block the SAME turn more than once.

Its trigger is a marker FILE, not something in the reply — so once it blocks,
it blocks again on the continuation, and again, until the harness overrides it
with a user-visible "a hook blocked the turn from ending 9 consecutive times"
warning. Exit 2 and `hookSpecificOutput.additionalContext` feed the SAME
counter in the agent loop, so a hook using either needs the guard the harness
prescribes: check `stop_hook_active` and succeed while it is true.

One block is the gate doing its job. More is noise, and noise aimed at the
user, which the standing hook axiom forbids.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_GUARD = (
    Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "deliverable_gate_guard.py"
)


def _decide(payload: dict, sessions_root: Path) -> int:
    spec = importlib.util.spec_from_file_location("dgg", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._decide(payload, sessions_root)


def _blocking_marker(root: Path, sid: str) -> None:
    d = root / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "deliverable.json").write_text(
        json.dumps({"session_id": sid, "status": "rendered_unverified"})
    )


def test_it_blocks_the_first_time(tmp_path):
    sid = "a1b2c3d4-0000-4000-8000-000000000000"
    _blocking_marker(tmp_path, sid)
    assert _decide({"session_id": sid}, tmp_path) == 2


def test_it_does_NOT_block_again_while_the_turn_is_already_continuing(tmp_path):
    """The bound. Without it this reaches the cap and surfaces to the user."""
    sid = "a1b2c3d4-0000-4000-8000-000000000000"
    _blocking_marker(tmp_path, sid)
    assert _decide({"session_id": sid, "stop_hook_active": True}, tmp_path) == 0
