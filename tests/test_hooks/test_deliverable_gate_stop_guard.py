"""The deliverable gate keeps blocking until the MARKER changes — not until the
turn continues once.

This file exists because the opposite was nearly shipped. Both Stop hooks in
this repo feed the same block counter, so the temptation is to give both the
same `stop_hook_active` guard. That is right for `genesis_stop_hook.py`, whose
nudges are ADVISORY — a reminder said twice adds nothing — and wrong here.

This hook is a GATE. `.claude/skills/deliverable-builder/SKILL.md` calls its
pipeline "*non-skippable*" and instructs the model: "the Stop-hook enforces
this; don't fight it — pass the gate or `cancel` the deliverable". A gate that
releases after one block enforces nothing: the session ends with an unverified
artifact as soon as the model ACKNOWLEDGES the warning, which is exactly the
outcome the gate exists to prevent.

So the release conditions are the marker's own — `verified` or `cancelled`,
both one edit away and both under the model's control — plus the staleness
escape for an abandoned marker. `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` is the last
of them, and reaching it is the correct signal: a session that rendered a
deliverable and refused to verify it eight times SHOULD surface.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

_GUARD = (
    Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "deliverable_gate_guard.py"
)


def _decide(payload: dict, sessions_root: Path) -> int:
    spec = importlib.util.spec_from_file_location("dgg", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._decide(payload, sessions_root)


def _marker(root: Path, sid: str, status: str = "rendered_unverified") -> Path:
    d = root / sid
    d.mkdir(parents=True, exist_ok=True)
    p = d / "deliverable.json"
    p.write_text(json.dumps({"session_id": sid, "status": status}))
    return p


_SID = "a1b2c3d4-0000-4000-8000-000000000000"


def test_it_blocks_the_first_time(tmp_path):
    _marker(tmp_path, _SID)
    assert _decide({"session_id": _SID}, tmp_path) == 2


def test_it_KEEPS_blocking_while_the_turn_is_already_continuing(tmp_path):
    """The gate's whole point. Acknowledging a block is not passing it.

    `stop_hook_active` is true on the continuation the previous block caused.
    Allowing there would end the session with the artifact unverified — the
    model need only say "understood" once.
    """
    _marker(tmp_path, _SID)
    assert _decide({"session_id": _SID, "stop_hook_active": True}, tmp_path) == 2


def test_a_verified_marker_releases_it(tmp_path):
    """Release condition 1 — and it holds mid-continuation, which is the point.

    The escape from the block is passing Gate 2, not waiting the block out.
    """
    _marker(tmp_path, _SID, status="verified")
    assert _decide({"session_id": _SID, "stop_hook_active": True}, tmp_path) == 0


def test_a_cancelled_marker_releases_it(tmp_path):
    """Release condition 2 — abandoning the deliverable deliberately."""
    _marker(tmp_path, _SID, status="cancelled")
    assert _decide({"session_id": _SID, "stop_hook_active": True}, tmp_path) == 0


def test_a_stale_marker_releases_it(tmp_path):
    """Release condition 3 — an abandoned marker must not wedge Stop forever.

    Asserted mid-continuation too: staleness is the escape hatch that does not
    depend on the model doing anything, so it must not itself be gated on the
    turn being the first one.
    """
    import os

    spec = importlib.util.spec_from_file_location("dgg", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    p = _marker(tmp_path, _SID)
    old = time.time() - (mod._STALE_SECONDS + 60)
    os.utime(p, (old, old))
    assert _decide({"session_id": _SID, "stop_hook_active": True}, tmp_path) == 0
