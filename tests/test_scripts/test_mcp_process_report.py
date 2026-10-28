"""Ownership claims must survive missing data and PID/parent races."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import mcp_process_report as report


def process(root, pid, ppid, ticks, argv, *, metrics=True):
    path = root / str(pid)
    path.mkdir(exist_ok=True)
    # Field 2 deliberately exercises comm containing spaces and parentheses.
    (path / "stat").write_text(f"{pid} (a name ) ) S {ppid} " + "0 " * 17 + str(ticks))
    (path / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    if metrics:
        (path / "status").write_text("Name:\tprivate\nVmRSS:\t12 kB\n")
        (path / "smaps_rollup").write_text("Pss: 8 kB\nSwapPss: 2 kB\n")
    return path


@pytest.fixture
def proc(tmp_path):
    boot = tmp_path / "sys/kernel/random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text("fixture-boot")
    return tmp_path


def test_ancestry_is_not_conversation_ownership_and_output_is_private(proc):
    process(proc, 10, 1, 20, ["/bin/codex", "app-server", "--secret=never-print-me"])
    process(proc, 11, 10, 21, ["/bin/python", "/private/genesis_mcp_server.py", "health"])
    process(proc, 12, 11, 22, ["/bin/worker", "private prompt"])
    process(proc, 13, 1, 23, ["/bin/python", "/private/genesis_mcp_server.py", "memory"])
    result = report.snapshot(proc)
    rows = {r["pid"]: r for r in result["processes"]}
    assert set(rows) == {10, 11, 12, 13}
    assert [rows[p]["daemon_pid"] for p in rows] == [10, 10, 10, None]
    assert rows[13]["process_ownership"] == "unknown"
    assert all(r["thread_ownership"] == "unknown" for r in rows.values())
    assert result["thread_inventory"]["status"] == "unavailable"
    assert rows[11]["boot_id"] == "fixture-boot" and rows[11]["start_ticks"] == 21
    assert result["memory_totals"]["pss_kib"] == {"sum_available": 32, "measured_count": 4, "selected_count": 4}
    assert result["memory_totals"]["rss_kib"]["sum_available"] == 48
    assert result["memory_totals"]["swap_pss_kib"]["sum_available"] == 8
    serialized = json.dumps(result)
    assert "never-print-me" not in serialized and "private prompt" not in serialized
    assert "/private" not in serialized


def test_missing_memory_is_unknown_and_scan_discloses_discovery_errors(proc):
    process(proc, 10, 1, 20, ["codex", "app-server"], metrics=False)
    (proc / "11").mkdir()
    result = report.snapshot(proc)
    assert not result["discovery_complete"]
    assert result["discovery_errors"][0]["pid"] == 11
    assert result["memory_totals"]["pss_kib"] == {"sum_available": None, "measured_count": 0, "selected_count": 1}
    assert len(result["processes"][0]["memory_errors"]) == 2


@pytest.mark.parametrize("change", ["reuse", "exit", "reparent", "parent_reuse"])
def test_races_remove_ownership_and_unstable_metrics(proc, monkeypatch, change):
    process(proc, 10, 1, 20, ["codex", "app-server"])
    process(proc, 11, 10, 21, ["python", "genesis_mcp_server.py"])
    original = report.proc_identity
    counts = {}

    def read(path):
        pid = int(path.name)
        counts[pid] = counts.get(pid, 0) + 1
        if counts[pid] > 1 and pid == (10 if change == "parent_reuse" else 11):
            if change == "exit":
                raise FileNotFoundError
            return (99, 21) if change == "reparent" else (1, 500)
        return original(path)

    monkeypatch.setattr(report, "proc_identity", read)
    rows = {r["pid"]: r for r in report.snapshot(proc)["processes"]}
    assert rows[11]["daemon_pid"] is None
    unstable = rows[10 if change == "parent_reuse" else 11]
    assert all(value is None for value in unstable["memory"].values())


def test_parent_start_after_child_and_cycles_do_not_prove_ownership(proc):
    process(proc, 10, 1, 100, ["codex", "app-server"])
    process(proc, 11, 10, 20, ["python", "genesis_mcp_server.py"])
    process(proc, 12, 13, 20, ["python", "genesis_mcp_server.py"])
    process(proc, 13, 12, 20, ["python", "genesis_mcp_server.py"])
    rows = {r["pid"]: r for r in report.snapshot(proc)["processes"]}
    assert all(rows[pid]["daemon_pid"] is None for pid in (11, 12, 13))


def test_missing_boot_id_permission_denial_and_invalid_metrics(proc, monkeypatch):
    (proc / "sys/kernel/random/boot_id").unlink()
    path = process(proc, 10, 1, 20, ["codex", "app-server"])
    (path / "status").write_text("VmRSS: -1 kB\n")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.name == "smaps_rollup":
            raise PermissionError
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    result = report.snapshot(proc)
    row = result["processes"][0]
    assert row["daemon_pid"] is None and row["boot_id"] is None
    assert row["memory"]["rss_kib"] is None
    assert "smaps_rollup:PermissionError" in row["memory_errors"]
    assert not result["discovery_complete"]


@pytest.mark.parametrize("argv", [
    ["bash", "-c", "codex app-server"],
    ["python", "unrelated.py", "genesis_mcp_server.py"],
    ["codex", "exec", "app-server"], [],
])
def test_arbitrary_arguments_are_not_server_identity(argv):
    assert report.family(argv) is None


def test_cli_explicit_cgroup_and_empty_scope(proc):
    cgroup = proc / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.max").write_text("max")
    (cgroup / "memory.current").write_text("512")
    result = subprocess.run(
        [sys.executable, report.__file__, "--proc-root", str(proc), "--cgroup", str(cgroup)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    assert data["processes"] == []
    assert data["memory_totals"]["pss_kib"]["sum_available"] is None
    assert data["cgroup"]["values"] == {"memory.current": "512", "memory.max": "max"}
    assert "memory.pressure" in data["cgroup"]["errors"]


def test_unreadable_proc_root_aborts(tmp_path):
    with pytest.raises(FileNotFoundError):
        report.snapshot(tmp_path / "absent")
