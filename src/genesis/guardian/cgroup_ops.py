# GROUNDWORK(guardian-cgroup): Emergency I/O relief infrastructure for host-side
# recovery when the container is frozen and incus exec is unresponsive. Wire into
# recovery.py when I/O stall detection triggers automated relief.
"""Cgroup operations — HOST-SIDE. Direct cgroup v2 access for I/O relief and process management.

Provides host-side escape valves when the container is frozen and incus exec
is unresponsive:
  - I/O pressure reading from PSI files
  - Dynamic io.max relief to unblock D-state processes
  - Container PID enumeration via cgroup
  - Top I/O consumer identification via /proc/PID/io
  - Host-side process kill

All paths use the cgroup v2 layout: /sys/fs/cgroup/lxc.payload.{container}/
"""

from __future__ import annotations

import logging
import signal
from pathlib import Path

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.health_signals import parse_psi_content
from genesis.util.proc_io import rank_by_io_rate, read_proc_io

logger = logging.getLogger(__name__)

CGROUP_BASE = "/sys/fs/cgroup/lxc.payload.{container}"


def _cgroup_path(container: str) -> Path:
    """Return the cgroup v2 base path for a container."""
    return Path(CGROUP_BASE.format(container=container))


def read_io_pressure(container: str) -> dict[str, float] | None:
    """Read io.pressure from the container's cgroup filesystem.

    Returns a dict with keys like 'some_avg10', 'some_avg60', 'full_avg10',
    'full_avg60', etc. Returns None on any error.
    """
    psi_path = _cgroup_path(container) / "io.pressure"
    try:
        content = psi_path.read_text()
        result = parse_psi_content(content)
        return result if result else None
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read io.pressure for %s: %s", container, exc)
        return None


async def relieve_io_max(container: str) -> bool:
    """Write 'max' to io.max to remove any residual hard I/O limits.

    This is the D-state escape valve. When io.max throttling causes all
    processes to enter D-state (unkillable even with SIGKILL), the only
    recovery is to modify io.max. Writing 'max' removes all limits.

    Requires sudo because the cgroup is owned by root.
    Returns True on success.
    """
    io_max_path = _cgroup_path(container) / "io.max"
    try:
        # Read current io.max to log what we're relieving
        if io_max_path.exists():
            current = io_max_path.read_text().strip()
            logger.info("Current io.max for %s: %s", container, current)

        # Write via sudo sh -c with quoted path (prevents shell injection)
        import shlex
        rc, stdout, stderr = await _run_subprocess(
            "sudo", "sh", "-c", f'echo max > {shlex.quote(str(io_max_path))}',
            timeout=10.0,
        )
        if rc != 0:
            logger.error(
                "Failed to relieve io.max for %s: %s", container, stderr,
            )
            return False

        logger.info("Relieved io.max for %s — all I/O limits removed", container)
        return True
    except Exception as exc:
        logger.error("Failed to relieve io.max for %s: %s", container, exc)
        return False


async def read_swap_max(container: str) -> str | None:
    """Read memory.swap.max from the container's cgroup via sudo.

    Read through sudo rather than directly: cgroup-delegation ownership of the
    lxc.payload subtree varies, and a perm-denied plain read would be
    indistinguishable from "knob absent" (the same reason
    scripts/lib/container_swap.sh reads with ``sudo cat``). Returns the trimmed
    value ("0", "max", or a byte count) or None when unreadable/absent
    (container stopped, cgroup v1, non-standard layout) — None means
    "no signal", never "healthy".
    """
    swap_path = _cgroup_path(container) / "memory.swap.max"
    try:
        rc, stdout, _stderr = await _run_subprocess(
            "sudo", "cat", str(swap_path), timeout=10.0,
        )
        if rc != 0:
            return None
        return stdout.strip() or None
    except Exception as exc:
        logger.warning("Failed to read memory.swap.max for %s: %s", container, exc)
        return None


async def activate_swap_max(container: str) -> bool:
    """Write 'max' to memory.swap.max — live-activate swap on a RUNNING container.

    incus applies ``limits.memory.swap`` only when the container STARTS, so on
    an already-running container the live cgroup keeps ``memory.swap.max=0``
    until the next restart and every memory spike becomes the load-100 D-state
    OOM-thrash wedge instead of degrading into swap. Writing ``max`` mirrors
    what incus does at start (scripts/lib/container_swap.sh does the same from
    host-setup); this is the guardian-side equivalent for installs that never
    re-run host-setup.

    Requires sudo because the cgroup is owned by root. Returns True on success.
    """
    swap_path = _cgroup_path(container) / "memory.swap.max"
    try:
        import shlex
        rc, _stdout, stderr = await _run_subprocess(
            "sudo", "sh", "-c", f"echo max > {shlex.quote(str(swap_path))}",
            timeout=10.0,
        )
        if rc != 0:
            logger.error(
                "Failed to activate memory.swap.max for %s: %s", container, stderr,
            )
            return False
        logger.info("Activated swap live for %s (memory.swap.max=max)", container)
        return True
    except Exception as exc:
        logger.error("Failed to activate memory.swap.max for %s: %s", container, exc)
        return False


def list_container_pids(container: str) -> list[int]:
    """List all PIDs in the container's cgroup.

    Reads cgroup.procs from the container's cgroup hierarchy. Returns an
    empty list on any error.
    """
    procs_path = _cgroup_path(container) / "cgroup.procs"
    try:
        content = procs_path.read_text()
        return [int(line.strip()) for line in content.splitlines() if line.strip().isdigit()]
    except (OSError, ValueError) as exc:
        logger.warning("Failed to list PIDs for %s: %s", container, exc)
        return []


# Shared with the container watchdog; one sampler for both callers.
_read_proc_io = read_proc_io


def find_top_io_pids(container: str, top_n: int = 5) -> list[dict]:
    """Find top I/O consumers among container PIDs (cumulative).

    Reads /proc/PID/io for each container PID and returns the top N by
    total bytes (read + write). These are CUMULATIVE lifetime counts —
    long-running processes dominate. For current-rate ranking, use
    find_top_io_pids_rate().

    Each entry is a dict with keys:
    pid, read_bytes, write_bytes, total_bytes, comm.

    Returns an empty list on any error. Individual PID read failures are
    silently skipped (process may have exited).
    """
    pids = list_container_pids(container)
    if not pids:
        return []

    io_data = [d for pid in pids if (d := _read_proc_io(pid)) is not None]
    io_data.sort(key=lambda x: x["total_bytes"], reverse=True)
    return io_data[:top_n]


def find_top_io_pids_rate(
    container: str, top_n: int = 5, sample_interval_s: float = 0.5,
) -> list[dict]:
    """Find top I/O consumers by current rate (delta sampling).

    Takes two /proc/PID/io snapshots separated by sample_interval_s and
    computes the byte delta. Identifies the process actively writing NOW,
    not just the one with the highest cumulative total.

    Each entry is a dict with keys:
      pid, comm, read_rate, write_rate, total_rate (bytes/sec),
      read_bytes_cumulative, write_bytes_cumulative.

    PIDs that disappear between samples are silently skipped.
    Returns an empty list on any error.
    """
    pids = list_container_pids(container)
    if not pids:
        return []
    rates, _readable, _total, _churn = rank_by_io_rate(pids, top_n, sample_interval_s)
    return rates


async def kill_pid(
    pid: int, sig: int = signal.SIGKILL, *, container: str = "",
) -> bool:
    """Kill a process via sudo kill.

    Uses sudo because container processes are owned by the container's
    uid mapping. Safety checks:
      - pid > 1 (prevents init kill)
      - If container is specified, verifies the PID belongs to that
        container's cgroup before killing (prevents host process kill)
    Returns True on success.
    """
    if pid <= 1:
        logger.error("Refusing to kill pid %d — too dangerous", pid)
        return False

    if container:
        cgroup_pids = list_container_pids(container)
        if pid not in cgroup_pids:
            logger.error(
                "Refusing to kill pid %d — not in container %s cgroup (%d pids listed)",
                pid, container, len(cgroup_pids),
            )
            return False

    try:
        rc, stdout, stderr = await _run_subprocess(
            "sudo", "kill", f"-{sig}", str(pid),
            timeout=10.0,
        )
        if rc != 0:
            logger.warning("Failed to kill pid %d: %s", pid, stderr)
            return False

        logger.info("Killed pid %d with signal %d", pid, sig)
        return True
    except Exception as exc:
        logger.error("Failed to kill pid %d: %s", pid, exc)
        return False
