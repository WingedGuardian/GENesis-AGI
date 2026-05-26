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

import contextlib
import logging
import signal
import time
from pathlib import Path

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.health_signals import parse_psi_content

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


def _read_proc_io(pid: int) -> dict | None:
    """Read /proc/PID/io and /proc/PID/comm for a single PID.

    Returns a dict with read_bytes, write_bytes, total_bytes, comm,
    or None if the PID is gone or unreadable.
    """
    try:
        io_path = Path(f"/proc/{pid}/io")
        if not io_path.exists():
            return None

        io_content = io_path.read_text()
        read_bytes = 0
        write_bytes = 0
        for line in io_content.splitlines():
            if line.startswith("read_bytes:"):
                read_bytes = int(line.split(":")[1].strip())
            elif line.startswith("write_bytes:"):
                write_bytes = int(line.split(":")[1].strip())

        comm = "unknown"
        with contextlib.suppress(OSError):
            comm = Path(f"/proc/{pid}/comm").read_text().strip()

        return {
            "pid": pid,
            "read_bytes": read_bytes,
            "write_bytes": write_bytes,
            "total_bytes": read_bytes + write_bytes,
            "comm": comm,
        }
    except (OSError, ValueError):
        return None


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

    # First sample
    t0: dict[int, dict] = {}
    for pid in pids:
        data = _read_proc_io(pid)
        if data:
            t0[pid] = data

    if not t0:
        return []

    time.sleep(sample_interval_s)

    # Second sample + delta
    rates: list[dict] = []
    for pid, before in t0.items():
        after = _read_proc_io(pid)
        if after is None:
            continue  # PID disappeared between samples
        delta_read = max(0, after["read_bytes"] - before["read_bytes"])
        delta_write = max(0, after["write_bytes"] - before["write_bytes"])
        delta_total = delta_read + delta_write
        rates.append({
            "pid": pid,
            "comm": after["comm"],
            "read_rate": delta_read / sample_interval_s,
            "write_rate": delta_write / sample_interval_s,
            "total_rate": delta_total / sample_interval_s,
            "read_bytes_cumulative": after["read_bytes"],
            "write_bytes_cumulative": after["write_bytes"],
        })

    rates.sort(key=lambda x: x["total_rate"], reverse=True)
    return rates[:top_n]


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
