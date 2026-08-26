"""Tests for deliverable_gate_guard: a marker stuck at rendered_unverified past
24h is treated as abandoned (allow with a warning) instead of wedging Stop forever.

(full_suite_guard's selector/targeting tests moved to test_full_suite_guard.py when
that hook was rewritten to block whole-directory runs.)
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS = _WORKTREE / "scripts" / "hooks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_dgg = _load("deliverable_gate_guard")


# --- deliverable_gate_guard staleness escape ---


def _write_marker(root: Path, sid: str, status: str, age_seconds: float = 0.0) -> Path:
    d = root / sid
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "deliverable.json"
    marker.write_text(json.dumps({"session_id": sid, "status": status}))
    if age_seconds:
        past = time.time() - age_seconds
        os.utime(marker, (past, past))
    return marker


class TestDeliverableStaleness:
    def test_fresh_rendered_unverified_blocks(self, tmp_path):
        _write_marker(tmp_path, "sess1", "rendered_unverified", age_seconds=60)
        assert _dgg._decide({"session_id": "sess1"}, tmp_path) == 2

    def test_stale_rendered_unverified_allows(self, tmp_path):
        _write_marker(tmp_path, "sess1", "rendered_unverified", age_seconds=25 * 3600)
        assert _dgg._decide({"session_id": "sess1"}, tmp_path) == 0

    def test_just_under_threshold_still_blocks(self, tmp_path):
        _write_marker(tmp_path, "sess1", "rendered_unverified", age_seconds=23 * 3600)
        assert _dgg._decide({"session_id": "sess1"}, tmp_path) == 2

    def test_verified_status_always_allows(self, tmp_path):
        _write_marker(tmp_path, "sess1", "verified", age_seconds=60)
        assert _dgg._decide({"session_id": "sess1"}, tmp_path) == 0

    def test_no_marker_allows(self, tmp_path):
        assert _dgg._decide({"session_id": "sess1"}, tmp_path) == 0

    def test_other_session_marker_never_blocks(self, tmp_path):
        _write_marker(tmp_path, "sess1", "rendered_unverified", age_seconds=60)
        assert _dgg._decide({"session_id": "sess2"}, tmp_path) == 0
