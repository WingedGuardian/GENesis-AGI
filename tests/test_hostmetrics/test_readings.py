"""Readers in genesis.hostmetrics.readings, against synthetic cgroup/proc trees."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.hostmetrics import readings as r

GIB = 1024**3


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def cg(tmp_path):
    return tmp_path / "cgroup"


@pytest.fixture
def proc(tmp_path):
    return tmp_path / "proc"


# ── memory readers (moved from genesis.runtime.cgroup) ────────────────────────
def test_memory_max_v2_value_and_unlimited(cg):
    _tree(cg, {"memory.max": "16000000000\n"})
    assert r.read_container_memory_max(cg) == 16000000000
    _tree(cg, {"memory.max": "max\n"})
    assert r.read_container_memory_max(cg) is None


def test_memory_max_v1_fallback(cg):
    _tree(cg, {"memory/memory.limit_in_bytes": "16000000000"})
    assert r.read_container_memory_max(cg) == 16000000000


def test_memory_max_v1_unlimited_sentinel_is_none(cg):
    _tree(cg, {"memory/memory.limit_in_bytes": "9223372036854771712"})
    assert r.read_container_memory_max(cg) is None


def test_memory_current_v2_then_v1(cg):
    _tree(cg, {"memory/memory.usage_in_bytes": "500"})
    assert r.read_container_memory_current(cg) == 500
    _tree(cg, {"memory.current": "700"})
    assert r.read_container_memory_current(cg) == 700


def test_reclaimable_v2_excludes_shmem(cg):
    # v2 reclaimable = inactive_file + active_file (file LRU), NOT the `file`
    # type-counter, which also counts tmpfs/shmem on the anon LRU.
    _tree(cg, {"memory.stat": "file 5000\ninactive_file 2000\nactive_file 1800\nshmem 1200\n"})
    assert r.read_container_memory_reclaimable(cg) == 3800


def test_reclaimable_v1_fallback(cg):
    _tree(cg, {"memory/memory.stat": "total_inactive_file 1000\ntotal_active_file 500\nrss 2\n"})
    assert r.read_container_memory_reclaimable(cg) == 1500


def test_readers_return_none_on_missing_tree(cg):
    assert r.read_container_memory_max(cg) is None
    assert r.read_container_memory_current(cg) is None
    assert r.read_container_memory_reclaimable(cg) is None


# ── combined memory view (same definition as cc/session_cap.effective_memory) ─
def _meminfo(proc: Path, total_kib: int, avail_kib: int) -> None:
    _tree(
        proc,
        {"meminfo": f"MemTotal: {total_kib} kB\nMemFree: 1 kB\nMemAvailable: {avail_kib} kB\n"},
    )


def test_read_memory_cgroup_limit_caps_host_procfs(cg, proc):
    _meminfo(proc, 64 * 1024 * 1024, 60 * 1024 * 1024)  # procfs shows a bigger host
    _tree(
        cg,
        {
            "memory.max": str(16 * GIB),
            "memory.current": str(10 * GIB),
            "memory.stat": f"inactive_file {GIB}\nactive_file {GIB}\n",
        },
    )
    m = r.read_memory(cg, proc)
    assert m.source == "cgroup"
    assert m.total == 16 * GIB
    assert m.available == 8 * GIB  # limit 16 − current 10 + file LRU 2


def test_read_memory_clamped_by_procfs_available(cg, proc):
    # procfs MemAvailable below the cgroup figure (dirty cache, a tighter host):
    # the smaller one wins, as in session_cap.effective_memory.
    _meminfo(proc, 64 * 1024 * 1024, 3 * 1024 * 1024)
    _tree(cg, {"memory.max": str(16 * GIB), "memory.current": str(10 * GIB)})
    assert r.read_memory(cg, proc).available == 3 * GIB


def test_read_memory_unknown_when_limit_finite_but_usage_unreadable(cg, proc):
    # procfs may describe HOST headroom here; trusting it would over-admit.
    _meminfo(proc, 64 * 1024 * 1024, 60 * 1024 * 1024)
    _tree(cg, {"memory.max": str(16 * GIB)})
    assert r.read_memory(cg, proc) is None


def test_read_memory_procfs_when_no_limit(cg, proc):
    _meminfo(proc, 8 * 1024 * 1024, 4 * 1024 * 1024)
    _tree(cg, {"memory.max": "max"})
    m = r.read_memory(cg, proc)
    assert (m.source, m.total, m.available) == ("procfs", 8 * GIB, 4 * GIB)


# ── CPU ───────────────────────────────────────────────────────────────────────
def test_cpu_capacity_from_quota(cg, monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(8)))
    _tree(cg, {"cpu.max": "250000 100000"})
    assert r.cpu_capacity(cg) == 2.5


def test_cpu_capacity_unlimited_quota_uses_affinity(cg, monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(6)))
    _tree(cg, {"cpu.max": "max 200000"})
    assert r.cpu_capacity(cg) == 6.0


def test_cpu_capacity_quota_never_exceeds_affinity(cg, monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(2)))
    _tree(cg, {"cpu.max": "800000 100000"})
    assert r.cpu_capacity(cg) == 2.0


class _FakeTime:
    """sleep() advances the clock and rewrites the counter, like real time passing."""

    def __init__(self, on_sleep):
        self.now = 100.0
        self._on_sleep = on_sleep

    def clock(self):
        return self.now

    def sleep(self, secs):
        self.now += secs
        self._on_sleep()


def test_cpu_used_from_cgroup_usage_delta(cg, proc):
    _tree(cg, {"cpu.stat": "usage_usec 1000000\n"})
    fake = _FakeTime(lambda: _tree(cg, {"cpu.stat": "usage_usec 7000000\n"}))
    # 6 CPU-seconds over a 2 s window = 3 cores busy
    assert r.read_cpu_used(2.0, cg, proc, sleep=fake.sleep, clock=fake.clock) == 3.0


def test_cpu_used_falls_back_to_proc_stat(cg, proc):
    tck = os.sysconf("SC_CLK_TCK")
    # user nice system idle iowait irq softirq steal
    _tree(proc, {"stat": f"cpu  {tck} 0 0 {50 * tck} 0 0 0 0 0 0\ncpu0 1 0 0 1\n"})
    fake = _FakeTime(
        lambda: _tree(proc, {"stat": f"cpu  {3 * tck} 0 {tck} {60 * tck} {tck} 0 0 0 0 0\n"})
    )
    # busy grew by 3 s (user +2, system +1); iowait is idle time. Over 1 s = 3 cores.
    assert r.read_cpu_used(1.0, cg, proc, sleep=fake.sleep, clock=fake.clock) == 3.0


@pytest.mark.parametrize("line", ["cpu  1 2 3 4", "cpu  1 2 x 4 5 6 7 8"])
def test_malformed_proc_stat_is_unreadable_not_a_crash(cg, proc, line):
    _tree(proc, {"stat": line + "\n"})
    fake = _FakeTime(lambda: None)
    assert r.read_cpu_used(1.0, cg, proc, sleep=fake.sleep, clock=fake.clock) is None


def test_cpu_used_none_when_unreadable(cg, proc):
    fake = _FakeTime(lambda: None)
    assert r.read_cpu_used(1.0, cg, proc, sleep=fake.sleep, clock=fake.clock) is None


# ── PSI ───────────────────────────────────────────────────────────────────────
_PSI = "some avg10=1.00 avg60=2.00 avg300={v} total=1\nfull avg10=0 avg60=0 avg300=0 total=0\n"


def test_psi_prefers_cgroup_file_over_proc(cg, proc):
    # /proc/pressure is host-wide inside a container; the cgroup file is ours.
    _tree(cg, {"memory.pressure": _PSI.format(v="12.50")})
    _tree(proc, {"pressure/memory": _PSI.format(v="99.00")})
    assert r.read_psi("memory", cg, proc) == 12.5


def test_psi_falls_back_to_proc(cg, proc):
    _tree(proc, {"pressure/io": _PSI.format(v="3.25")})
    assert r.read_psi("io", cg, proc) == 3.25


def test_psi_none_when_absent(cg, proc):
    assert r.read_psi("cpu", cg, proc) is None


# ── disk ──────────────────────────────────────────────────────────────────────
def test_disk_reads_statvfs(tmp_path):
    total, free = r.read_disk(tmp_path)
    assert total > 0 and 0 <= free <= total


def test_disk_matches_os_statvfs(tmp_path):
    st = os.statvfs(tmp_path)
    assert r.read_disk(tmp_path) == (st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize)


def test_disk_path_not_yet_created_reads_its_parent(tmp_path):
    # A job often writes into a directory it creates; judge the parent's filesystem.
    assert r.read_disk(tmp_path / "new" / "deeper") == r.read_disk(tmp_path)
    assert r.disk_device(tmp_path / "new") == os.stat(tmp_path).st_dev


# ── the move keeps the old import path, and the package stays light ──────────
def test_runtime_cgroup_reexports_the_moved_readers():
    from genesis.runtime import cgroup

    for name in (
        "read_container_memory_max",
        "read_container_memory_current",
        "read_container_memory_reclaimable",
    ):
        assert getattr(cgroup, name) is getattr(r, name)
        assert name in cgroup.__all__
    assert not hasattr(cgroup, "_read_text")  # private helpers moved, not re-exported


def test_import_does_not_load_the_runtime():
    code = (
        "import sys, genesis.hostmetrics.readings, genesis.hostmetrics.preflight, "
        "genesis.hostmetrics.host, genesis.hostmetrics.__main__; "
        "print(sorted(m for m in sys.modules "
        "if m.startswith(('genesis.runtime', 'genesis.guardian', 'yaml'))))"
    )
    src = str(Path(r.__file__).resolve().parents[2])  # this tree's src, not main's
    env = {**os.environ, "PYTHONPATH": src}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )
    assert out.stdout.strip() == "[]"
