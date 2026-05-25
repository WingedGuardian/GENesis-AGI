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
from pathlib import Path

from genesis.guardian.health_signals import _run_subprocess

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
        result: dict[str, float] = {}
        for line in content.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            prefix = parts[0]  # "some" or "full"
            for part in parts[1:]:
                if "=" in part:
                    key, _, val = part.partition("=")
                    with contextlib.suppress(ValueError):
                        result[f"{prefix}_{key}"] = float(val)
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


def find_top_io_pids(container: str, top_n: int = 5) -> list[dict]:
    """Find top I/O consumers among container PIDs.

    Reads /proc/PID/io for each container PID and returns the top N by
    total bytes (read + write). Each entry is a dict with keys:
    pid, read_bytes, write_bytes, total_bytes, comm.

    Returns an empty list on any error. Individual PID read failures are
    silently skipped (process may have exited).
    """
    pids = list_container_pids(container)
    if not pids:
        return []

    io_data: list[dict] = []
    for pid in pids:
        try:
            io_path = Path(f"/proc/{pid}/io")
            comm_path = Path(f"/proc/{pid}/comm")

            if not io_path.exists():
                continue

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
                comm = comm_path.read_text().strip()

            io_data.append({
                "pid": pid,
                "read_bytes": read_bytes,
                "write_bytes": write_bytes,
                "total_bytes": read_bytes + write_bytes,
                "comm": comm,
            })
        except (OSError, ValueError):
            # Process may have exited between listing and reading
            continue

    # Sort by total I/O, descending
    io_data.sort(key=lambda x: x["total_bytes"], reverse=True)
    return io_data[:top_n]


async def kill_pid(pid: int, sig: int = signal.SIGKILL) -> bool:
    """Kill a process via sudo kill.

    Uses sudo because container processes are owned by the container's
    uid mapping. Validates pid > 1 to prevent catastrophic kills.
    Returns True on success.
    """
    if pid <= 1:
        logger.error("Refusing to kill pid %d — too dangerous", pid)
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
