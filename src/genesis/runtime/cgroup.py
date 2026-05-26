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


def read_container_memory_max() -> int | None:
    """Read the container's memory.max from the root cgroup."""
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw == "max":
            return None
        return int(raw)
    except (OSError, ValueError):
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
