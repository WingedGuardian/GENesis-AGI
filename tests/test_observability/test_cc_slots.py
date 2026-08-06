"""Tests for per-CC-slot RSS enumeration (PR-2c leak detection)."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from genesis.observability import cc_slots
from genesis.observability.cc_slots import (
    SLOT_RSS_CRIT_MB,
    SLOT_RSS_WARN_MB,
    enumerate_cc_slots,
    read_proc_rss_mb,
    read_proc_start_iso,
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


# ── start-time (btime + /proc/<pid>/stat field 22) ──────────────────────────


def _write_btime(root, btime: int):
    """Fake /proc/stat carrying a btime line (plus noise lines)."""
    (root / "stat").write_text(
        f"cpu  1 2 3 4\nbtime {btime}\nprocesses 12345\n"
    )


def _write_stat(root, pid: int, comm: str, starttime_ticks: int):
    """Fake /proc/<pid>/stat. comm is parenthesised and may contain spaces/parens
    — field 22 (starttime) sits 19 tokens after the LAST ')'."""
    d = root / str(pid)
    d.mkdir(exist_ok=True)
    # fields 3..21 (state..itrealvalue) = 19 placeholder tokens, then field 22.
    tail = " ".join(["0"] * 19 + [str(starttime_ticks)])
    (d / "stat").write_text(f"{pid} ({comm}) {tail} 0 0 0\n")


class TestReadProcStartIso:
    def test_computes_wall_start_from_btime_and_ticks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        btime = 1_700_000_000
        ticks = 500_000
        _write_btime(tmp_path, btime)
        _write_stat(tmp_path, 4242, "claude", ticks)
        clk = os.sysconf("SC_CLK_TCK")
        expected = datetime.fromtimestamp(btime + ticks / clk, UTC).isoformat()
        assert read_proc_start_iso(4242) == expected

    def test_comm_with_inner_parens_and_spaces(self, tmp_path, monkeypatch):
        # The comm field itself contains ')' and a space; rsplit(')',1) must
        # still land on field 22, not choke on the inner paren.
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        btime = 1_700_000_000
        ticks = 12_345
        _write_btime(tmp_path, btime)
        _write_stat(tmp_path, 4243, "weird (name) proc", ticks)
        clk = os.sysconf("SC_CLK_TCK")
        expected = datetime.fromtimestamp(btime + ticks / clk, UTC).isoformat()
        assert read_proc_start_iso(4243) == expected

    def test_missing_pid_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _write_btime(tmp_path, 1_700_000_000)
        assert read_proc_start_iso(999_999) is None

    def test_missing_btime_returns_none(self, tmp_path, monkeypatch):
        # No /proc/stat btime → cannot anchor → None (never a bogus time).
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _write_stat(tmp_path, 4244, "claude", 500_000)
        assert read_proc_start_iso(4244) is None

    def test_malformed_stat_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _write_btime(tmp_path, 1_700_000_000)
        (tmp_path / "4245").mkdir()
        (tmp_path / "4245" / "stat").write_text("garbage no parens here\n")
        assert read_proc_start_iso(4245) is None

    def test_enumerate_includes_started_at(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        btime = 1_700_000_000
        ticks = 777_000
        _write_btime(tmp_path, btime)
        _make_proc_entry(tmp_path, 5001, "claude", "1", 800_000)
        _write_stat(tmp_path, 5001, "claude", ticks)
        clk = os.sysconf("SC_CLK_TCK")
        expected = datetime.fromtimestamp(btime + ticks / clk, UTC).isoformat()
        rows = enumerate_cc_slots()
        assert rows[0]["started_at"] == expected

    def test_enumerate_started_at_none_when_stat_absent(self, tmp_path, monkeypatch):
        # Existing-test compatibility: no stat/btime → started_at present as None,
        # never raising and never dropping the slot.
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _make_proc_entry(tmp_path, 5002, "claude", "1", 800_000)
        rows = enumerate_cc_slots()
        assert rows[0]["started_at"] is None
