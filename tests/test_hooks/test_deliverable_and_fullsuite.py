"""Tests for the two small C-item guard fixes (PR-Guards):

* deliverable_gate_guard: a marker stuck at rendered_unverified past 24h is
  treated as abandoned (allow with a warning) instead of wedging Stop forever.
* full_suite_guard: a -k/-m selector run is a targeted subset, not the full
  suite (cosmetic false-positive fix).
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
_fsg = _load("full_suite_guard")


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


# --- full_suite_guard -k/-m selector ---


class TestFullSuiteSelector:
    def test_bare_pytest_is_full_suite(self):
        assert _fsg._is_full_suite("pytest") is True

    def test_pytest_v_is_full_suite(self):
        assert _fsg._is_full_suite("pytest -v") is True

    def test_k_selector_not_full_suite(self):
        assert _fsg._is_full_suite("pytest -k test_foo") is False

    def test_k_selector_quoted_expr(self):
        assert _fsg._is_full_suite('pytest -k "test_foo or test_bar"') is False

    def test_m_selector_not_full_suite(self):
        assert _fsg._is_full_suite("pytest -m slow") is False

    def test_k_equals_form(self):
        assert _fsg._is_full_suite("pytest -k=test_foo") is False

    def test_keyword_long_form(self):
        assert _fsg._is_full_suite("pytest --keyword=test_foo") is False

    def test_path_still_recognized(self):
        assert _fsg._is_full_suite("pytest tests/test_x.py") is False

    def test_glued_k_selector(self):
        """Glued short form `-kfoo` is a selector, not the full suite (NOTE 4)."""
        assert _fsg._is_full_suite("pytest -kfoo") is False

    def test_glued_m_selector(self):
        assert _fsg._is_full_suite("pytest -mslow") is False

    def test_k_without_value_is_full_suite(self):
        """A dangling -k with no value is not a real selection."""
        assert _fsg._is_full_suite("pytest -k") is True

    def test_tb_flag_alone_is_full_suite(self):
        """--tb=short with no path/selector is still the full suite."""
        assert _fsg._is_full_suite("pytest --tb=short") is True
