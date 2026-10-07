"""Container and host readings: memory, CPU, pressure (PSI), disk.

Stdlib only, and importable without ``genesis.runtime`` (whose package init
pulls the whole runtime graph), so system python3 and lightweight hooks can
use it. Every reader takes its cgroup root and procfs root as arguments, so
tests run against synthetic trees.

Inside a container the cgroup root is the container's own: its limits are the
container's limits. ``/proc`` may describe the HOST there (``/proc/meminfo``,
``/proc/pressure``), which is why cgroup files are always read first.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")

# cgroup v1 "unlimited" sentinel (PAGE_COUNTER_MAX, page-aligned). memory.
# limit_in_bytes reports a value at/above this when no limit is set.
_CGROUP_V1_UNLIMITED = 0x7FFFFFFFFFFFF000


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_stat(path: Path) -> dict[str, int]:
    raw = _read_text(path)
    stats: dict[str, int] = {}
    if raw:
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                stats[parts[0]] = int(parts[1])
    return stats


def read_container_memory_max(root: Path = CGROUP_ROOT) -> int | None:
    """Container memory limit in bytes (cgroup v2, then v1). None = unlimited/unknown."""
    raw = _read_text(root / "memory.max")  # v2 unified
    if raw is not None:
        if raw == "max":
            return None
        try:
            return int(raw)
        except ValueError:
            return None
    v1 = _read_int(root / "memory/memory.limit_in_bytes")  # v1 fallback
    if v1 is not None and 0 < v1 < _CGROUP_V1_UNLIMITED:
        return v1
    return None


def read_container_memory_current(root: Path = CGROUP_ROOT) -> int | None:
    """Container current memory usage (bytes) — cgroup v2, then v1."""
    cur = _read_int(root / "memory.current")  # v2
    if cur is not None:
        return cur
    return _read_int(root / "memory/memory.usage_in_bytes")  # v1


def read_container_memory_reclaimable(root: Path = CGROUP_ROOT) -> int | None:
    """Reclaimable file-backed page cache (bytes) from cgroup memory.stat — v2 then v1.

    memory.current counts page cache as "used", so max-current UNDER-states
    available. Adding back the reclaimable file cache mirrors what /proc
    MemAvailable means (usable without swapping). We sum the file LRU lists
    (`inactive_file` + `active_file`) rather than the type-based `file` counter:
    on cgroup v2, `file` also includes tmpfs/shmem pages, which sit on the ANON
    LRU and are NOT reclaimable for a new anonymous allocation — counting them
    would over-state available and could let the gate ALLOW a session the
    container cannot actually hold (kernel cgroup-v2 memory.stat semantics:
    inactive_file + active_file == page cache minus tmpfs). The file LRU still
    contains dirty pages (writeback-then-reclaim, not instant), but the caller
    additionally clamps with `min(procfs_MemAvailable, …)`, so the estimate stays
    conservative. v1's `total_*_file` counters are already list-based."""
    v2 = _read_stat(root / "memory.stat")  # v2: file LRU lists (exclude shmem)
    if "inactive_file" in v2 or "active_file" in v2:
        return v2.get("inactive_file", 0) + v2.get("active_file", 0)
    v1 = _read_stat(root / "memory/memory.stat")  # v1: active+inactive file
    if "total_inactive_file" in v1 or "total_active_file" in v1:
        return v1.get("total_inactive_file", 0) + v1.get("total_active_file", 0)
    return None


@dataclass(frozen=True)
class Memory:
    total: int  # bytes
    available: int  # bytes
    source: str  # "cgroup" or "procfs"


def read_meminfo(proc: Path = PROC_ROOT) -> tuple[int, int] | None:
    """(MemTotal, MemAvailable) in bytes, or None if either is missing."""
    fields: dict[str, int] = {}
    for line in (_read_text(proc / "meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable") and rest.split():
            try:
                fields[key] = int(rest.split()[0]) * 1024
            except ValueError:
                return None
    if len(fields) < 2:
        return None
    return fields["MemTotal"], fields["MemAvailable"]


def read_memory(root: Path = CGROUP_ROOT, proc: Path = PROC_ROOT) -> Memory | None:
    """Memory total and available, capped by the cgroup limit.

    Same definition as ``cc/session_cap.effective_memory``: available = limit −
    current + file LRU, clamped by procfs MemAvailable. A finite limit with
    unreadable usage returns None (unknown): procfs there may be the host's.
    """
    meminfo = read_meminfo(proc)
    limit = read_container_memory_max(root)
    if limit:
        current = read_container_memory_current(root)
        if current is None:
            return None
        available = max(0, limit - current + (read_container_memory_reclaimable(root) or 0))
        total = limit
        if meminfo:
            total = min(total, meminfo[0])
            available = min(available, meminfo[1])
        return Memory(total, available, "cgroup")
    if meminfo is None:
        return None
    return Memory(meminfo[0], meminfo[1], "procfs")


def cpu_capacity(root: Path = CGROUP_ROOT) -> float:
    """Usable CPUs: the cgroup ``cpu.max`` quota, capped by the affinity mask."""
    try:
        cpus = float(len(os.sched_getaffinity(0)) or 1)
    except (AttributeError, OSError):
        cpus = float(os.cpu_count() or 1)
    quota, _, period = (_read_text(root / "cpu.max") or "max").partition(" ")
    if quota != "max":
        try:
            return min(cpus, int(quota) / int(period or 100000))
        except (ValueError, ZeroDivisionError):
            pass
    return cpus


def _cpu_usage_usec(root: Path, proc: Path) -> float | None:
    usage = _read_stat(root / "cpu.stat").get("usage_usec")
    if usage is not None:
        return float(usage)
    # No cgroup v2 cpu.stat: busy time from /proc/stat's aggregate line
    # (user nice system idle iowait irq softirq steal …; guest is inside user).
    first = (_read_text(proc / "stat") or "").split("\n", 1)[0].split()
    if len(first) < 6 or first[0] != "cpu":
        return None
    try:
        vals = [int(v) for v in first[1:9]]
    except ValueError:
        return None
    # Minus idle, iowait and steal: cgroup usage_usec counts none of them, and
    # hypervisor steal is time this system did NOT run.
    busy = sum(vals) - vals[3] - vals[4] - (vals[7] if len(vals) > 7 else 0)
    return busy * 1e6 / os.sysconf("SC_CLK_TCK")


def read_cpu_used(
    window: float = 3.0,
    root: Path = CGROUP_ROOT,
    proc: Path = PROC_ROOT,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> float | None:
    """CPUs busy (in cores), averaged over ``window`` seconds. None if unreadable."""
    before, t0 = _cpu_usage_usec(root, proc), clock()
    sleep(window)
    after, t1 = _cpu_usage_usec(root, proc), clock()
    if before is None or after is None or t1 <= t0:
        return None
    return max(0.0, (after - before) / 1e6 / (t1 - t0))


def read_psi(resource: str, root: Path = CGROUP_ROOT, proc: Path = PROC_ROOT) -> float | None:
    """The ``some avg300`` pressure percent for cpu, memory or io.

    The cgroup's own file first: ``/proc/pressure`` is host-wide inside a
    container, so pairing it with container usage would mix two scopes.
    """
    for path in (root / f"{resource}.pressure", proc / "pressure" / resource):
        for line in (_read_text(path) or "").splitlines():
            if line.startswith("some "):
                for field in line.split()[1:]:
                    key, _, value = field.partition("=")
                    if key == "avg300":
                        try:
                            return float(value)
                        except ValueError:
                            return None
    return None


def existing_parent(path: Path | str) -> Path:
    """``path`` or its nearest existing ancestor: a job often writes into a
    directory it has yet to create, on the filesystem of its parent."""
    p = Path(path).expanduser().absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def disk_device(path: Path | str) -> int | None:
    """The device id of the filesystem that would hold ``path``."""
    try:
        return os.stat(existing_parent(path)).st_dev
    except OSError:
        return None


def read_disk(path: Path | str) -> tuple[int, int] | None:
    """(total, free-to-unprivileged) bytes of the filesystem that would hold ``path``."""
    try:
        st = os.statvfs(existing_parent(path))
    except OSError:
        return None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
