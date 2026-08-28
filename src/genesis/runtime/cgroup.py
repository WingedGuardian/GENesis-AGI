"""Cgroup v2 utilities — container-side.

Provides cgroup state reading for health reporting and device detection
for I/O throttling. The actual I/O isolation is handled by systemd-run
transient scopes (see cc/invoker.py), not by cgroup subtree management.

Design spec: docs/superpowers/specs/2026-05-25-cgroup-io-resilience.md
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _read_own_cgroup() -> Path | None:
    """Read /proc/self/cgroup to find our cgroup v2 path."""
    try:
        content = Path("/proc/self/cgroup").read_text().strip()
        for line in content.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                return Path("/sys/fs/cgroup") / parts[2].lstrip("/")
        return None
    except OSError as exc:
        logger.warning("Cannot read /proc/self/cgroup: %s", exc)
        return None


def detect_root_device() -> str | None:
    """Detect the block device major:minor for the root filesystem.

    For io.max / IOReadBandwidthMax, we need the REAL block device —
    not the virtual device from os.stat("/").st_dev.

    Parses /proc/self/mountinfo for the root mount's device.
    """
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text()
        for line in mountinfo.splitlines():
            fields = line.split()
            if len(fields) >= 10 and fields[4] == "/":
                dev = fields[2]
                logger.info("Root filesystem device from mountinfo: %s", dev)
                return dev
        return None
    except OSError as exc:
        logger.warning("Cannot parse mountinfo for root device: %s", exc)
        return None


# cgroup v1 "unlimited" sentinel (PAGE_COUNTER_MAX, page-aligned). memory.
# limit_in_bytes reports a value at/above this when no limit is set.
_CGROUP_V1_UNLIMITED = 0x7FFFFFFFFFFFF000


def _read_text(path: str) -> str | None:
    """Read a cgroup file (seam so the v1/v2 fallbacks are unit-testable)."""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_stat(path: str) -> dict[str, int]:
    raw = _read_text(path)
    stats: dict[str, int] = {}
    if raw:
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                stats[parts[0]] = int(parts[1])
    return stats


def read_container_memory_max() -> int | None:
    """Container memory limit in bytes (cgroup v2, then v1). None = unlimited/unknown."""
    raw = _read_text("/sys/fs/cgroup/memory.max")  # v2 unified
    if raw is not None:
        if raw == "max":
            return None
        try:
            return int(raw)
        except ValueError:
            return None
    v1 = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")  # v1 fallback
    if v1 is not None and 0 < v1 < _CGROUP_V1_UNLIMITED:
        return v1
    return None


def read_container_memory_current() -> int | None:
    """Container current memory usage (bytes) — cgroup v2, then v1."""
    cur = _read_int("/sys/fs/cgroup/memory.current")  # v2
    if cur is not None:
        return cur
    return _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")  # v1


def read_container_memory_reclaimable() -> int | None:
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
    v2 = _read_stat("/sys/fs/cgroup/memory.stat")  # v2: file LRU lists (exclude shmem)
    if "inactive_file" in v2 or "active_file" in v2:
        return v2.get("inactive_file", 0) + v2.get("active_file", 0)
    v1 = _read_stat("/sys/fs/cgroup/memory/memory.stat")  # v1: active+inactive file
    if "total_inactive_file" in v1 or "total_active_file" in v1:
        return v1.get("total_inactive_file", 0) + v1.get("total_active_file", 0)
    return None


def cgroup_status() -> dict:
    """Return cgroup information for health reporting."""
    result: dict = {"scope": None, "device": None, "memory_max": None}

    scope = _read_own_cgroup()
    if scope:
        result["scope"] = str(scope)

    result["device"] = detect_root_device()
    result["memory_max"] = read_container_memory_max()

    return result
