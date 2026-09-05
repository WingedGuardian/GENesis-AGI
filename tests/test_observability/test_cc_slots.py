"""Tests for per-CC-slot RSS enumeration (PR-2c leak detection)."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from genesis.observability import cc_slots, mcp_spawn_store
from genesis.observability.cc_slots import (
    SLOT_RSS_CRIT_MB,
    SLOT_RSS_WARN_MB,
    enumerate_cc_slots,
    read_proc_rss_mb,
    read_proc_start_iso,
    slot_status,
)

# Interactive slots run WITHOUT -p; every headless `claude -p` cognitive/background
# call runs WITH it. cmdline is NUL-separated argv.
_INTERACTIVE_CMD = b"claude\x00--dangerously-skip-permissions\x00"
_PRINT_CMD = b"claude\x00-p\x00do a thing\x00--model\x00sonnet\x00"


@pytest.fixture(autouse=True)
def _isolate_spawn_dir(tmp_path, monkeypatch):
    """enumerate_cc_slots reads the mcp-spawn file plane for slot labels; point it
    at an empty tmp so tests never read the real ~/.genesis/mcp-spawn. Tests that
    exercise the plane override _SPAWN_DIR again (last setattr wins)."""
    monkeypatch.setattr(mcp_spawn_store, "_SPAWN_DIR", tmp_path / "_spawn_isolated")


def _make_proc_entry(
    root,
    pid: int,
    comm: str,
    slot: str | None,
    rss_kb: int | None,
    cmdline: bytes | None = _INTERACTIVE_CMD,
):
    d = root / str(pid)
    d.mkdir()
    (d / "comm").write_text(comm + "\n")
    if slot is not None:
        (d / "environ").write_bytes(b"PATH=/usr/bin\x00GENESIS_SLOT=" + slot.encode() + b"\x00LANG=C\x00")
    else:
        (d / "environ").write_bytes(b"PATH=/usr/bin\x00LANG=C\x00")
    if cmdline is not None:
        (d / "cmdline").write_bytes(cmdline)
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
    def test_includes_interactive_labels_and_excludes_cognitive(self, tmp_path, monkeypatch):
        # Comm-based: label from environ when readable; an interactive claude with
        # NO slot is still a session (slot=None); a headless `claude -p` cognitive
        # call is NOT a session and is excluded.
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _make_proc_entry(tmp_path, 1001, "claude", "3", 870_000)          # slot 3, healthy
        _make_proc_entry(tmp_path, 1002, "node", "3", 120_000)            # not claude → skip
        _make_proc_entry(tmp_path, 1003, "claude", None, 500_000)         # interactive, no slot → INCLUDED (slot None)
        # Sized RELATIVE to the constant, not a magic 7 GB: the thresholds are
        # explicitly tunable, and a literal here silently re-aims the assertion
        # at "degraded" the next time they move (which is exactly what happened
        # when they were rebased onto the whole-tree denominator).
        _make_proc_entry(
            tmp_path, 1004, "claude", "7", (SLOT_RSS_CRIT_MB + 1024) * 1024
        )  # slot 7, over CRIT
        _make_proc_entry(
            tmp_path, 1005, "claude", None, 400_000, cmdline=_PRINT_CMD
        )  # cognitive `claude -p` → EXCLUDED
        (tmp_path / "notapid").mkdir()  # non-numeric entry ignored

        rows = enumerate_cc_slots()
        by_pid = {r["pid"]: r for r in rows}
        assert set(by_pid) == {1001, 1003, 1004}
        assert by_pid[1001]["slot"] == "3" and by_pid[1001]["status"] == "healthy"
        assert by_pid[1001]["rss_mb"] == round(870_000 / 1024, 1)
        assert by_pid[1003]["slot"] is None  # interactive but unregistered
        assert by_pid[1004]["slot"] == "7" and by_pid[1004]["status"] == "error"

    def test_slot_label_from_spawn_plane_when_environ_absent(self, tmp_path, monkeypatch):
        # The server-sandbox case: environ carries no readable GENESIS_SLOT, but the
        # mcp-spawn file plane maps pid→slot. The label must come from the plane.
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        spawn = tmp_path / "spawn"
        spawn.mkdir()
        (spawn / "5").write_text(f"4242 {'a' * 40} 2026-08-06T00:00:00+00:00\n")
        monkeypatch.setattr(mcp_spawn_store, "_SPAWN_DIR", spawn)
        _make_proc_entry(tmp_path, 4242, "claude", None, 700_000)  # no environ slot
        rows = enumerate_cc_slots()
        assert len(rows) == 1
        assert rows[0]["pid"] == 4242 and rows[0]["slot"] == "5"

    def test_spawn_plane_label_dropped_when_pid_recycled(self, tmp_path, monkeypatch):
        # The plane maps pid 4242 -> slot 5 with an ancient spawn_at, but this live
        # interactive process started long AFTER it (pid recycled by the OS) → the
        # stale label must be dropped (slot None); the process is still listed.
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        spawn = tmp_path / "spawn"
        spawn.mkdir()
        (spawn / "5").write_text(f"4242 {'a' * 40} 1970-01-01T00:00:00+00:00\n")
        monkeypatch.setattr(mcp_spawn_store, "_SPAWN_DIR", spawn)
        _write_btime(tmp_path, 1_700_000_000)  # recent boot → proc start >> 1970 spawn_at
        _make_proc_entry(tmp_path, 4242, "claude", None, 700_000)
        _write_stat(tmp_path, 4242, "claude", 500_000)
        rows = enumerate_cc_slots()
        assert len(rows) == 1
        assert rows[0]["pid"] == 4242
        assert rows[0]["slot"] is None

    def test_stale_spawn_slot_on_headless_pid_is_excluded(self, tmp_path, monkeypatch):
        # A stale spawn-file maps a slot to a pid the OS has since reused for a
        # headless `claude -p` cognitive call. cmdline is authoritative → the call
        # must be EXCLUDED, never labeled with the stale slot as a live session.
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        spawn = tmp_path / "spawn"
        spawn.mkdir()
        (spawn / "3").write_text(f"7777 {'a' * 40} 2026-08-06T00:00:00+00:00\n")
        monkeypatch.setattr(mcp_spawn_store, "_SPAWN_DIR", spawn)
        # pid 7777 is now a headless cognitive call (cmdline has -p)
        _make_proc_entry(tmp_path, 7777, "claude", None, 500_000, cmdline=_PRINT_CMD)
        assert enumerate_cc_slots() == []

    def test_two_claude_same_slot_label_both_kept_pid_keyed(self, tmp_path, monkeypatch):
        # Rows are keyed by pid now (each live claude = one row); a shared slot label
        # (anomalous) surfaces BOTH rather than silently hiding one.
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _make_proc_entry(tmp_path, 2001, "claude", "2", 500_000)
        _make_proc_entry(tmp_path, 2002, "claude", "2", 900_000)
        rows = enumerate_cc_slots()
        assert {r["pid"] for r in rows} == {2001, 2002}
        assert all(r["slot"] == "2" for r in rows)

    def test_ordering_numeric_then_unlabeled(self, tmp_path, monkeypatch):
        # "10" follows "9"; unlabeled (None) interactive sessions sort last.
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        _make_proc_entry(tmp_path, 3001, "claude", "9", 800_000)
        _make_proc_entry(tmp_path, 3002, "claude", "10", 800_000)
        _make_proc_entry(tmp_path, 3003, "claude", "2", 800_000)
        _make_proc_entry(tmp_path, 3004, "claude", None, 800_000)  # unlabeled → last
        assert [r["slot"] for r in enumerate_cc_slots()] == ["2", "9", "10", None]

    def test_empty_proc_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        assert enumerate_cc_slots() == []

    def test_missing_proc_dir_returns_empty_not_raise(self, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", "/nonexistent/proc/path")
        assert enumerate_cc_slots() == []


class TestIsInteractive:
    def _cmdline(self, tmp_path, monkeypatch, pid, raw):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "cmdline").write_bytes(raw)

    def test_interactive_no_print_flag(self, tmp_path, monkeypatch):
        self._cmdline(tmp_path, monkeypatch, 10, _INTERACTIVE_CMD)
        assert cc_slots._is_interactive(10) is True

    def test_short_print_flag_excluded(self, tmp_path, monkeypatch):
        self._cmdline(tmp_path, monkeypatch, 11, _PRINT_CMD)
        assert cc_slots._is_interactive(11) is False

    def test_long_print_flag_excluded(self, tmp_path, monkeypatch):
        self._cmdline(tmp_path, monkeypatch, 12, b"claude\x00--print\x00hi\x00")
        assert cc_slots._is_interactive(12) is False

    def test_permissions_flag_not_mistaken_for_print(self, tmp_path, monkeypatch):
        # exact-arg match: --dangerously-skip-permissions must NOT match -p
        self._cmdline(tmp_path, monkeypatch, 13, b"claude\x00--dangerously-skip-permissions\x00")
        assert cc_slots._is_interactive(13) is True

    def test_unreadable_cmdline_is_non_interactive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))
        assert cc_slots._is_interactive(999_999) is False


# ── start-time (btime + /proc/<pid>/stat field 22) ──────────────────────────


def _write_btime(root, btime: int):
    """Fake /proc/stat carrying a btime line (plus noise lines)."""
    (root / "stat").write_text(
        f"cpu  1 2 3 4\nbtime {btime}\nprocesses 12345\n"
    )


def _write_stat(root, pid: int, comm: str, starttime_ticks: int, ppid: int = 0):
    """Fake /proc/<pid>/stat. comm is parenthesised and may contain spaces/parens
    — after the LAST ')' the tokens start at field 3 (state), which puts ppid
    (field 4) at index 1 and starttime (field 22) at index 19."""
    d = root / str(pid)
    d.mkdir(exist_ok=True)
    # fields 3..21 (state..itrealvalue) = 19 placeholder tokens, then field 22.
    _fields = ["0"] * 19
    _fields[1] = str(ppid)  # field 4 = ppid
    tail = " ".join(_fields + [str(starttime_ticks)])
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


# ── whole-tree accounting (a slot is not one process) ───────────────────────


def _tree_fixture(tmp_path, monkeypatch, root_rss_kb: int, children: dict[int, int]):
    """A `claude` slot at pid 100 plus non-claude children hanging off it.

    `children` maps pid -> rss_kb, each parented to 100 unless the pid is also a
    key in another child's subtree (callers build deeper shapes by hand).
    """
    _write_btime(tmp_path, 1_700_000_000)
    _make_proc_entry(tmp_path, 100, "claude", "1", root_rss_kb)
    _write_stat(tmp_path, 100, "claude", 500_000, ppid=1)
    for pid, rss_kb in children.items():
        _make_proc_entry(tmp_path, pid, "python", None, rss_kb, cmdline=b"python\x00-m\x00srv\x00")
        _write_stat(tmp_path, pid, "python", 500_000, ppid=100)
    monkeypatch.setattr(cc_slots, "_PROC", str(tmp_path))


class TestSlotTreeRss:
    """A slot carries a Serena LSP and a fleet of MCP servers as children; they
    are most of its memory. Counting only the root understates it ~3x."""

    def test_tree_sums_the_root_and_its_descendants(self, tmp_path, monkeypatch):
        _tree_fixture(tmp_path, monkeypatch, 800 * 1024, {101: 900 * 1024, 102: 300 * 1024})
        children = cc_slots.build_child_map([100, 101, 102])
        assert cc_slots.slot_tree_rss_mb(100, children) == pytest.approx(2000.0, abs=1.0)

    def test_tree_reaches_grandchildren(self, tmp_path, monkeypatch):
        # A depth-2 descendant must count: MCP servers spawn their own helpers.
        _tree_fixture(tmp_path, monkeypatch, 800 * 1024, {101: 900 * 1024})
        _make_proc_entry(tmp_path, 102, "node", None, 100 * 1024, cmdline=b"node\x00x\x00")
        _write_stat(tmp_path, 102, "node", 500_000, ppid=101)
        children = cc_slots.build_child_map([100, 101, 102])
        assert cc_slots.slot_tree_rss_mb(100, children) == pytest.approx(1800.0, abs=1.0)

    def test_a_ppid_cycle_terminates(self, tmp_path, monkeypatch):
        # A malformed/recycled ppid chain must not hang the dashboard thread.
        _tree_fixture(tmp_path, monkeypatch, 800 * 1024, {101: 100 * 1024})
        _write_stat(tmp_path, 100, "claude", 500_000, ppid=101)  # 100 -> 101 -> 100
        children = cc_slots.build_child_map([100, 101])
        assert cc_slots.slot_tree_rss_mb(100, children) == pytest.approx(900.0, abs=1.0)

    def test_an_unreadable_descendant_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        # A child that exits mid-walk contributes nothing and must not raise.
        _tree_fixture(tmp_path, monkeypatch, 800 * 1024, {101: 900 * 1024})
        (tmp_path / "102").mkdir()
        (tmp_path / "102" / "stat").write_text("102 (gone) 0 100 " + " ".join(["0"] * 18) + " 5\n")
        children = cc_slots.build_child_map([100, 101, 102])
        assert cc_slots.slot_tree_rss_mb(100, children) == pytest.approx(1700.0, abs=1.0)

    def test_row_reports_tree_as_rss_mb_and_root_as_proc_rss_mb(self, tmp_path, monkeypatch):
        _tree_fixture(tmp_path, monkeypatch, 800 * 1024, {101: 900 * 1024, 102: 300 * 1024})
        (row,) = enumerate_cc_slots()
        assert row["proc_rss_mb"] == pytest.approx(800.0, abs=1.0)
        assert row["rss_mb"] == pytest.approx(2000.0, abs=1.0)

    def test_status_is_decided_by_the_tree_not_the_root(self, tmp_path, monkeypatch):
        """THE regression this change exists for.

        Before, `status` and the awareness alert both keyed on the root process,
        so a slot whose tree had ballooned past the threshold still reported a
        small `claude` process and read healthy — the alert could not fire in
        the regime it was built for.
        """
        root_kb = 800 * 1024
        child_kb = (SLOT_RSS_WARN_MB + 200) * 1024  # tree well over WARN
        _tree_fixture(tmp_path, monkeypatch, root_kb, {101: child_kb})
        (row,) = enumerate_cc_slots()
        assert slot_status(row["proc_rss_mb"]) == "healthy", "root alone must look fine"
        assert row["rss_mb"] > SLOT_RSS_WARN_MB
        assert row["status"] == "degraded"

    def test_thresholds_are_ordered_and_tree_scaled(self):
        # Guards a careless edit: CRIT above WARN, and WARN above the observed
        # healthy-tree MAX (~3.5 GB — the distribution is bimodal, so the max,
        # not the mean, is what a normal slot can actually reach). Asserting
        # only the mean would let a normal working slot sit at WARN.
        assert SLOT_RSS_WARN_MB < SLOT_RSS_CRIT_MB
        assert SLOT_RSS_WARN_MB > 3500
