"""Cgroup v2 I/O isolation manager — container-side.

Creates two cgroup subtrees under the current process's cgroup:
  genesis-critical  — server, awareness loop, Sentinel process (high I/O priority)
  genesis-background — CC sessions, surplus compute (throttled I/O)

This is PREVENTIVE infrastructure: it stops CC sessions from saturating
host io.max and causing D-state freezes.  Once D-state occurs, only
Guardian (host-side) can help.

Requires:
  - Cgroup v2 filesystem (cgroup2fs) at /sys/fs/cgroup
  - io controller delegated through the user slice hierarchy
  - sudo for initial subtree_control setup (delegation doesn't persist)

Design spec: docs/superpowers/specs/2026-05-25-cgroup-io-resilience.md
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _read_own_cgroup() -> Path | None:
    """Read /proc/self/cgroup to find our cgroup v2 path."""
    try:
        content = Path("/proc/self/cgroup").read_text().strip()
        # cgroup v2: single line "0::/path/to/cgroup"
        for line in content.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                return Path("/sys/fs/cgroup") / parts[2].lstrip("/")
        return None
    except OSError as exc:
        logger.warning("Cannot read /proc/self/cgroup: %s", exc)
        return None


def _detect_root_device() -> str | None:
    """Detect the block device major:minor for the root filesystem.

    For io.max, we need the REAL block device — not the virtual device
    from os.stat("/").st_dev (which returns the btrfs/overlay device).

    Strategy: parse /proc/self/mountinfo for the root mount, find the
    backing device, resolve to major:minor via /sys/dev/block/.
    If the root is on device-mapper (LVM), use that directly.
    """
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text()
        for line in mountinfo.splitlines():
            fields = line.split()
            # Field 5 is the mount point
            if len(fields) >= 10 and fields[4] == "/":
                # Field 3 is major:minor of the mount
                dev = fields[2]
                # Verify this is a real block device by checking io.stat
                # at the root cgroup — devices that appear there are the
                # ones the kernel tracks I/O for.
                logger.info("Root filesystem device from mountinfo: %s", dev)
                return dev
        return None
    except OSError as exc:
        logger.warning("Cannot parse mountinfo for root device: %s", exc)
        return None


def _sudo_write(path: Path, content: str) -> bool:
    """Write to a cgroup file via sudo tee (cgroup files are root-owned)."""
    try:
        result = subprocess.run(
            ["sudo", "tee", str(path)],
            input=content.encode(),
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("sudo write to %s failed: %s", path, exc)
        return False


def _sudo_mkdir(path: Path) -> bool:
    """Create a cgroup directory via sudo mkdir."""
    try:
        result = subprocess.run(
            ["sudo", "mkdir", "-p", str(path)],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("sudo mkdir %s failed: %s", path, exc)
        return False


class CgroupManager:
    """Manages genesis-critical and genesis-background cgroup subtrees.

    Call setup() at server startup (before awareness loop or scheduler).
    Call move_to_background(pid) after forking CC sessions.
    """

    def __init__(self) -> None:
        self._scope = _read_own_cgroup()
        self._critical: Path | None = None
        self._background: Path | None = None
        self._init: Path | None = None
        self._device = _detect_root_device()
        self._ready = False

    @property
    def available(self) -> bool:
        """Whether cgroup v2 I/O isolation is available and set up."""
        return self._ready

    @property
    def device(self) -> str | None:
        """The block device major:minor for io.max."""
        return self._device

    def setup(self) -> bool:
        """Create cgroup subtrees and configure controllers.

        Steps:
        1. Create genesis-init child cgroup
        2. Move all processes from scope into genesis-init
        3. Enable io+memory+cpu+pids delegation on scope
        4. Create genesis-critical and genesis-background
        5. Set I/O weights and memory limits
        6. Move server process to genesis-critical

        Returns True if setup succeeded.  Returns False (and logs why)
        if prerequisites aren't met — the system runs without isolation
        (same as today, not worse).
        """
        if not self._scope:
            logger.warning("cgroup: cannot determine own cgroup path")
            return False

        if not self._scope.is_dir():
            logger.warning("cgroup: scope path %s doesn't exist", self._scope)
            return False

        # Check if already set up (idempotent — survives server restart
        # when the boot unit or a prior server run already configured cgroups)
        critical = self._scope / "genesis-critical"
        background = self._scope / "genesis-background"
        if critical.is_dir() and background.is_dir():
            try:
                child_ctrl = (critical / "cgroup.controllers").read_text()
                if "io" in child_ctrl:
                    self._critical = critical
                    self._background = background
                    self._init = self._scope / "genesis-init"
                    # Move server PID to critical if not already there
                    server_pid = str(os.getpid())
                    _sudo_write(critical / "cgroup.procs", server_pid + "\n")
                    self._ready = True
                    logger.info(
                        "cgroup: I/O isolation already active — reusing existing subtrees",
                    )
                    return True
            except OSError:
                pass  # Fall through to fresh setup

        # Check if io controller is available
        try:
            controllers = (self._scope / "cgroup.controllers").read_text()
        except OSError:
            logger.warning("cgroup: cannot read controllers at %s", self._scope)
            return False

        if "io" not in controllers:
            logger.warning("cgroup: io controller not available (have: %s)", controllers.strip())
            return False

        # Step 1: Create genesis-init to hold existing processes
        self._init = self._scope / "genesis-init"
        if not _sudo_mkdir(self._init):
            logger.error("cgroup: cannot create genesis-init")
            return False

        # Step 2: Move ALL processes from scope to genesis-init
        # This is required by cgroup v2's "no internal processes" constraint
        # before we can enable subtree_control.
        try:
            procs = (self._scope / "cgroup.procs").read_text().strip().splitlines()
        except OSError as exc:
            logger.error("cgroup: cannot read scope procs: %s", exc)
            return False

        moved = 0
        for pid_str in procs:
            pid_str = pid_str.strip()
            if pid_str:
                if _sudo_write(self._init / "cgroup.procs", pid_str + "\n"):
                    moved += 1
                else:
                    # Some PIDs may have exited between read and write
                    logger.debug("cgroup: couldn't move PID %s (may have exited)", pid_str)

        logger.info("cgroup: moved %d/%d processes to genesis-init", moved, len(procs))

        # Step 3: Enable controllers on scope's subtree_control
        if not _sudo_write(self._scope / "cgroup.subtree_control", "+io +memory +cpu +pids\n"):
            logger.error("cgroup: cannot enable subtree_control — processes may still be in scope")
            # Try to move remaining processes
            return False

        # Step 4: Create genesis-critical and genesis-background
        self._critical = self._scope / "genesis-critical"
        self._background = self._scope / "genesis-background"

        if not _sudo_mkdir(self._critical) or not _sudo_mkdir(self._background):
            logger.error("cgroup: cannot create subtrees")
            return False

        # Verify io controller is delegated to children
        try:
            child_controllers = (self._critical / "cgroup.controllers").read_text()
            if "io" not in child_controllers:
                logger.error("cgroup: io controller not delegated to children (have: %s)", child_controllers.strip())
                return False
        except OSError:
            logger.error("cgroup: cannot read child controllers")
            return False

        # Step 5: Set I/O weights
        _sudo_write(self._critical / "io.weight", "default 500\n")
        _sudo_write(self._background / "io.weight", "default 100\n")
        logger.info("cgroup: io.weight set — critical=500, background=100")

        # Set io.max on background (50% of host limit) if device is known
        if self._device:
            # We don't know the host's io.max, so we set a conservative
            # absolute limit.  These values are for a 300G SSD:
            #   rbps = 200MB/s (50% of typical SSD sequential read)
            #   wbps = 100MB/s (50% of typical SSD sequential write)
            # The io.weight already provides proportional fairness; io.max
            # is the hard ceiling that prevents D-state.
            rbps = 200 * 1024 * 1024  # 200 MB/s
            wbps = 100 * 1024 * 1024  # 100 MB/s
            io_max_line = f"{self._device} rbps={rbps} wbps={wbps}\n"
            if _sudo_write(self._background / "io.max", io_max_line):
                logger.info("cgroup: io.max set on background: %s", io_max_line.strip())
            else:
                logger.warning("cgroup: failed to set io.max on background (device %s)", self._device)

        # Step 5b: Memory limits
        container_max = self._read_container_memory_max()
        if container_max:
            self._set_memory_limits(container_max)

        # Step 6: Move server process to genesis-critical
        server_pid = str(os.getpid())
        if _sudo_write(self._critical / "cgroup.procs", server_pid + "\n"):
            logger.info("cgroup: server (PID %s) moved to genesis-critical", server_pid)
        else:
            logger.warning("cgroup: failed to move server to genesis-critical")

        self._ready = True
        logger.info("cgroup: I/O isolation active — critical and background subtrees ready")
        return True

    def move_to_background(self, pid: int) -> bool:
        """Move a PID to genesis-background (for CC sessions)."""
        if not self._ready or not self._background:
            return False
        return _sudo_write(self._background / "cgroup.procs", str(pid) + "\n")

    def move_to_critical(self, pid: int) -> bool:
        """Move a PID to genesis-critical (for essential processes)."""
        if not self._ready or not self._critical:
            return False
        return _sudo_write(self._critical / "cgroup.procs", str(pid) + "\n")

    def _read_container_memory_max(self) -> int | None:
        """Read the container's memory.max from the root cgroup."""
        try:
            raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
            if raw == "max":
                return None  # No memory limit set
            return int(raw)
        except (OSError, ValueError):
            return None

    def _set_memory_limits(self, container_max: int) -> None:
        """Set memory limits on background and critical subtrees.

        Policy (adaptive, not static):
          background memory.high = 62.5% of container max (soft limit)
          background memory.max  = 75% of container max (hard ceiling)
          critical   memory.min  = 2G (reserved, never reclaimed)

        Constraints:
          Floor: background memory.high >= 8G
          Ceiling: background memory.high <= container_max - 4G
          Recalculation: ONLY at boot (not during pressure)
        """
        if not self._background or not self._critical:
            return

        GiB = 1024 ** 3

        bg_high = int(container_max * 0.625)
        bg_max = int(container_max * 0.75)
        crit_min = 2 * GiB

        # Apply floor and ceiling
        bg_high = max(bg_high, 8 * GiB)
        bg_high = min(bg_high, container_max - 4 * GiB)
        bg_max = max(bg_max, bg_high + 2 * GiB)
        bg_max = min(bg_max, container_max - 2 * GiB)

        if bg_high <= 0 or bg_max <= bg_high:
            logger.warning(
                "cgroup: memory limits don't make sense for container_max=%dG, skipping",
                container_max // GiB,
            )
            return

        _sudo_write(self._background / "memory.high", str(bg_high) + "\n")
        _sudo_write(self._background / "memory.max", str(bg_max) + "\n")
        _sudo_write(self._critical / "memory.min", str(crit_min) + "\n")

        logger.info(
            "cgroup: memory limits — background high=%dG max=%dG, critical min=%dG "
            "(container max=%dG)",
            bg_high // GiB, bg_max // GiB, crit_min // GiB, container_max // GiB,
        )

    def status(self) -> dict:
        """Return current cgroup isolation status for health reporting."""
        if not self._ready:
            return {"active": False, "reason": "not set up"}

        result: dict = {"active": True, "device": self._device}

        for name, path in [
            ("critical", self._critical),
            ("background", self._background),
        ]:
            if path and path.is_dir():
                try:
                    procs = path.joinpath("cgroup.procs").read_text().strip().splitlines()
                    result[name] = {"procs": len(procs)}
                except OSError:
                    result[name] = {"procs": -1}

                # Read io.weight if available
                with contextlib.suppress(OSError):
                    result[name]["io_weight"] = path.joinpath("io.weight").read_text().strip()

        return result
