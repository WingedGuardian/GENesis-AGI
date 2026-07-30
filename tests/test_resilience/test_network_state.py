"""network_state store — tolerant read, atomic write, retention, staleness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.resilience import network_state


def test_read_absent_returns_none(tmp_path):
    assert network_state.read_state(tmp_path / "nope.json") is None


def test_read_corrupt_returns_none(tmp_path):
    p = tmp_path / "network_state.json"
    p.write_text("{not json")
    assert network_state.read_state(p) is None


def test_read_non_dict_returns_none(tmp_path):
    p = tmp_path / "network_state.json"
    p.write_text("[1, 2, 3]")
    assert network_state.read_state(p) is None


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "network_state.json"
    data = {"state": "OFFLINE", "last_probe_at": "2026-07-30T12:00:00+00:00"}
    network_state.write_state(data, p)
    got = network_state.read_state(p)
    assert got["state"] == "OFFLINE"


def test_write_caps_closed_windows(tmp_path):
    p = tmp_path / "network_state.json"
    windows = [{"start": str(i), "end": str(i), "cause": "all_fail"} for i in range(120)]
    network_state.write_state({"state": "NORMAL", "closed_windows": windows}, p)
    got = network_state.read_state(p)
    assert len(got["closed_windows"]) == network_state.MAX_CLOSED_WINDOWS
    # most-recent kept
    assert got["closed_windows"][-1]["start"] == "119"


def test_probe_age_fresh(tmp_path):
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    snap = {"last_probe_at": (now - timedelta(seconds=30)).isoformat()}
    age = network_state.probe_age_s(snap, now)
    assert 29 <= age <= 31


def test_probe_age_none_on_absent_or_garbled():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    assert network_state.probe_age_s(None, now) is None
    assert network_state.probe_age_s({}, now) is None
    assert network_state.probe_age_s({"last_probe_at": "not-a-date"}, now) is None
    assert network_state.probe_age_s({"last_probe_at": 12345}, now) is None


def test_probe_age_tolerates_naive_timestamp():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    snap = {"last_probe_at": "2026-07-30T11:59:00"}  # naive
    age = network_state.probe_age_s(snap, now)
    assert age is not None and 59 <= age <= 61
