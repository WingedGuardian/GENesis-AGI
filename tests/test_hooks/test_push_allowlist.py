"""Unit tests for scripts/hooks/push_allowlist.py — the local push allowlist.

The allowlist caches "branch X is confirmed on remote (push-urls U)" so a
re-push is decided offline instead of via a network ls-remote. These tests pin
the security-relevant invariants: it never matches an unrecorded branch, an
empty/disjoint url set never matches, stale entries expire, and every read/write
fails OPEN (a corrupt file can only cause a redundant prompt, never a phantom
allow).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
import push_allowlist as pa  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the allowlist state file under a tmp GENESIS_HOME (never ~/.genesis)."""
    gh = tmp_path / "genesis-home"
    gh.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GENESIS_HOME", str(gh))
    return gh


def _state(home: Path) -> Path:
    return home / "pushed_branches.json"


# ── roundtrip + keying ────────────────────────────────────────────────


def test_record_then_is_recorded_roundtrip(home):
    pa.record({"git@github.com:o/r.git"}, "feat/x")
    assert pa.is_recorded({"git@github.com:o/r.git"}, "feat/x") is True


def test_url_intersection_matches(home):
    pa.record({"url-a", "url-b"}, "feat/x")
    # A later push whose resolved url set overlaps the recorded set matches.
    assert pa.is_recorded({"url-b", "url-c"}, "feat/x") is True


def test_disjoint_urls_do_not_match(home):
    """Same branch NAME, a different repo's url → NOT recorded (no conflation)."""
    pa.record({"git@github.com:o/r.git"}, "feat/x")
    assert pa.is_recorded({"git@github.com:other/repo.git"}, "feat/x") is False


def test_empty_push_urls_never_matches_and_never_records(home):
    # is_recorded with no urls → False (fall back to ls-remote).
    pa.record({"url-a"}, "feat/x")
    assert pa.is_recorded(set(), "feat/x") is False
    # record with no urls → no-op (unresolvable remote can't be keyed).
    pa.record(set(), "feat/y")
    data = json.loads(_state(home).read_text())
    assert "feat/y" not in data["branches"]


def test_unrecorded_branch_not_recorded(home):
    pa.record({"url-a"}, "feat/x")
    assert pa.is_recorded({"url-a"}, "feat/never-pushed") is False


def test_empty_state_returns_false(home):
    # No file at all → not recorded (fail-open to prompt).
    assert pa.is_recorded({"url-a"}, "feat/x") is False


# ── REPLACE semantics (C2) ────────────────────────────────────────────


def test_rerecord_replaces_url_set(home):
    """A re-record REPLACES the url set (does not union) — a stale set-url --push
    ages out immediately rather than lingering in the trusted set."""
    pa.record({"old-url"}, "feat/x")
    pa.record({"new-url"}, "feat/x")
    data = json.loads(_state(home).read_text())
    assert data["branches"]["feat/x"]["urls"] == ["new-url"]
    assert pa.is_recorded({"old-url"}, "feat/x") is False
    assert pa.is_recorded({"new-url"}, "feat/x") is True


# ── freshness / prune ─────────────────────────────────────────────────


def _write_state(home: Path, branches: dict) -> None:
    _state(home).write_text(json.dumps({"version": 1, "branches": branches}))


def test_stale_entry_is_not_recorded(home):
    old = (datetime.now(UTC) - timedelta(days=pa.RETENTION_DAYS + 1)).isoformat()
    _write_state(home, {"feat/x": {"urls": ["url-a"], "ts": old}})
    assert pa.is_recorded({"url-a"}, "feat/x") is False


def test_fresh_entry_just_inside_window_is_recorded(home):
    recent = (datetime.now(UTC) - timedelta(days=pa.RETENTION_DAYS - 1)).isoformat()
    _write_state(home, {"feat/x": {"urls": ["url-a"], "ts": recent}})
    assert pa.is_recorded({"url-a"}, "feat/x") is True


def test_stale_entries_pruned_on_write(home):
    old = (datetime.now(UTC) - timedelta(days=pa.RETENTION_DAYS + 5)).isoformat()
    _write_state(home, {"stale/one": {"urls": ["u1"], "ts": old}})
    # Recording a different branch prunes the stale one in the same write.
    pa.record({"u2"}, "fresh/two")
    data = json.loads(_state(home).read_text())
    assert "stale/one" not in data["branches"]
    assert "fresh/two" in data["branches"]


def test_missing_or_unparseable_ts_treated_as_stale(home):
    _write_state(home, {"a": {"urls": ["u"]}, "b": {"urls": ["u"], "ts": "not-a-date"}})
    assert pa.is_recorded({"u"}, "a") is False
    assert pa.is_recorded({"u"}, "b") is False


# ── fail-open on corruption ───────────────────────────────────────────


def test_corrupt_file_fails_open_to_false(home):
    _state(home).write_text("{ this is not json ]]")
    assert pa.is_recorded({"url-a"}, "feat/x") is False


def test_record_overwrites_corrupt_file(home):
    _state(home).write_text("garbage")
    pa.record({"url-a"}, "feat/x")  # must not raise; overwrites with a valid envelope
    assert pa.is_recorded({"url-a"}, "feat/x") is True


def test_non_dict_envelope_fails_open(home):
    _state(home).write_text(json.dumps([1, 2, 3]))
    assert pa.is_recorded({"url-a"}, "feat/x") is False


# ── placement / hygiene ───────────────────────────────────────────────


def test_state_lands_under_genesis_home(home):
    pa.record({"url-a"}, "feat/x")
    assert _state(home).exists()


def test_no_tmp_files_left_behind(home):
    pa.record({"url-a"}, "feat/x")
    leftovers = list(home.glob(".pushed_branches.*.tmp"))
    assert leftovers == [], f"temp files not cleaned: {leftovers}"
