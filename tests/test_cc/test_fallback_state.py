"""Tests for the account-wide CC fallback-state record (genesis.cc.fallback_state)."""
from __future__ import annotations

import pytest

from genesis.cc import fallback_state as FS


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    # genesis_home() honors GENESIS_HOME — point it at a tmp dir so tests never
    # touch the real ~/.genesis/cc_fallback_state.json.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    return tmp_path


def test_read_missing_is_inactive():
    state = FS.read()
    assert state.is_fallback is False
    assert state == FS.FallbackState()


def test_enter_transitions_and_is_idempotent():
    assert FS.enter("claude", "glm-5.2", "rate_limit") is True  # transition
    s = FS.read()
    assert s.is_fallback is True
    assert (s.original, s.fallback, s.reason) == ("claude", "glm-5.2", "rate_limit")
    assert s.since  # timestamp stamped

    # Already active → no new transition (so only one ALERT fires).
    assert FS.enter("claude", "glm-5.2", "rate_limit") is False
    assert FS.read().since == s.since  # since preserved across refresh


def test_enter_refreshes_peer_without_new_transition():
    FS.enter("claude", "glm-5.2", "rate_limit")
    since = FS.read().since
    # A different peer mid-outage refreshes the record but is not a new transition.
    assert FS.enter("claude", "deepseek", "rate_limit") is False
    s = FS.read()
    assert s.fallback == "deepseek"
    assert s.since == since  # outage start unchanged


def test_clear_transitions_and_is_idempotent():
    FS.enter("claude", "glm-5.2", "rate_limit")
    assert FS.clear() is True  # active → inactive
    assert FS.read().is_fallback is False
    assert FS.clear() is False  # already inactive → no recovery ALERT


def test_corrupt_file_reads_inactive(tmp_path):
    (tmp_path / "cc_fallback_state.json").write_text("not-json{")
    assert FS.read().is_fallback is False


def test_non_dict_json_reads_inactive(tmp_path):
    (tmp_path / "cc_fallback_state.json").write_text("[1, 2, 3]")
    assert FS.read().is_fallback is False
