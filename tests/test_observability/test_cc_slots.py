"""Tests for per-CC-slot RSS enumeration (PR-2c leak detection)."""

from __future__ import annotations

import os

from genesis.observability import cc_slots
from genesis.observability.cc_slots import (
    SLOT_RSS_CRIT_MB,
    SLOT_RSS_WARN_MB,
    enumerate_cc_slots,
    read_proc_rss_mb,
    slot_status,
)


def _make_proc_entry(root, pid: int, comm: str, slot: str | None, rss_kb: int | None):
    d = root / str(pid)
    d.mkdir()
    (d / "comm").write_text(comm + "\n")
    if slot is not None:
        (d / "environ").write_bytes(b"PATH=/usr/bin\x00GENESIS_SLOT=" + slot.encode() + b"\x00LANG=C\x00")
    else:
        (d / "environ").write_bytes(b"PATH=/usr/bin\x00LANG=C\x00")
    if rss_kb is not None:
        (d / "status").write_text(f"Name:\t{comm}\nVmPeak:\t{rss_kb + 100} kB\nVmRSS:\t{rss_kb} kB\n")


class TestReadProcRssMb:
    def test_reads_own_process(self):
        # our own pid has a real VmRSS
        rss = read_proc_rss_mb(os.getpid())
        assert rss is not None and rss > 0

    def test_missing_pid_returns_none(self):
        assert read_proc_rss_mb(2_000_000_000) is None

    def test_parses_kb_to_mb(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        (tmp_path / "42").mkdir()
        (tmp_path / "42" / "status").write_text("VmRSS:\t2048 kB\n")
        assert read_proc_rss_mb(42) == 2.0


class TestSlotStatus:
    def test_thresholds(self):
        assert slot_status(900) == "healthy"
        assert slot_status(SLOT_RSS_WARN_MB - 1) == "healthy"
        assert slot_status(SLOT_RSS_WARN_MB) == "degraded"
        assert slot_status(SLOT_RSS_CRIT_MB - 1) == "degraded"
        assert slot_status(SLOT_RSS_CRIT_MB) == "error"


class TestEnumerateCcSlots:
    def test_filters_and_labels(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _make_proc_entry(tmp_path, 1001, "claude", "3", 870_000)          # slot 3, healthy
        _make_proc_entry(tmp_path, 1002, "node", "3", 120_000)            # not claude → skip
        _make_proc_entry(tmp_path, 1003, "claude", None, 500_000)         # no GENESIS_SLOT → skip
        _make_proc_entry(tmp_path, 1004, "claude", "7", 7 * 1024 * 1024)  # slot 7, CRIT (7 GB)
        (tmp_path / "notapid").mkdir()  # non-numeric entry ignored

        rows = enumerate_cc_slots()
        slots = {r["slot"]: r for r in rows}
        assert set(slots) == {"3", "7"}
        assert slots["3"]["status"] == "healthy"
        assert slots["3"]["rss_mb"] == round(870_000 / 1024, 1)
        assert slots["3"]["pid"] == 1001
        assert slots["7"]["status"] == "error"
        # sorted by slot label
        assert [r["slot"] for r in rows] == ["3", "7"]

    def test_two_claude_sharing_a_slot_larger_rss_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _make_proc_entry(tmp_path, 2001, "claude", "2", 500_000)
        _make_proc_entry(tmp_path, 2002, "claude", "2", 900_000)
        rows = enumerate_cc_slots()
        assert len(rows) == 1
        assert rows[0]["pid"] == 2002
        assert rows[0]["rss_mb"] == round(900_000 / 1024, 1)

    def test_numeric_slot_ordering(self, tmp_path, monkeypatch):
        # "10" must follow "9", not sort lexicographically before it
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _make_proc_entry(tmp_path, 3001, "claude", "9", 800_000)
        _make_proc_entry(tmp_path, 3002, "claude", "10", 800_000)
        _make_proc_entry(tmp_path, 3003, "claude", "2", 800_000)
        assert [r["slot"] for r in enumerate_cc_slots()] == ["2", "9", "10"]

    def test_empty_proc_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        assert enumerate_cc_slots() == []

    def test_missing_proc_dir_returns_empty_not_raise(self, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", "/nonexistent/proc/path")
        assert enumerate_cc_slots() == []
