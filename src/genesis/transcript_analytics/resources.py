"""Resource admission and enforced systemd scope for heavy analytics commands."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config

_CHILD = "GENESIS_TRANSCRIPT_RESOURCE_CHILD"


def _enforced(marker: str) -> bool:
    """Verify kernel limits in the current cgroup, never trust an env flag alone."""
    try:
        ram, cpu = (float(part) for part in marker.split(","))
        group = next(
            line[3:]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        root = Path("/sys/fs/cgroup") / group.lstrip("/")
        memory = int((root / "memory.max").read_text())
        swap = int((root / "memory.swap.max").read_text())
        quota, period = (int(v) for v in (root / "cpu.max").read_text().split())
        return 0 < memory <= ram and swap == 0 and 0 < quota / period * 100 <= cpu + 0.01
    except (OSError, ValueError, StopIteration, ZeroDivisionError):
        return False


def ensure_capped(argv: list[str], cfg: Config) -> int | None:
    """Return child exit code, or None only inside a verified capped child.

    argv is the Genesis CLI argument list (starting with ``transcripts``).
    75 means admission deferred; 69 means enforcement unavailable.
    """
    marker = os.environ.get(_CHILD)
    if marker is not None:
        if _enforced(marker):
            return None
        print(
            "transcript analytics unavailable: resource limits were not enforced", file=sys.stderr
        )
        return 69
    from genesis.hostmetrics.__main__ import take_snapshot
    from genesis.hostmetrics.preflight import Request, evaluate, load_levers

    snap = take_snapshot([], 0.5)
    if snap.memory is None:
        print("transcript analytics deferred: ASK (memory capacity unavailable)", file=sys.stderr)
        return 75
    ram = cfg.ram_bytes or max(1, int(snap.memory.total * cfg.ram_pct / 100))
    cpu = snap.cpu_capacity * cfg.cpu_pct
    result = evaluate(snap, Request("transcript analytics", ram=ram, cpu=cpu), load_levers())
    if result.verdict != "GO":
        reasons = "; ".join(c.reason for c in result.checks if c.verdict != "GO")
        print(f"transcript analytics deferred: {result.verdict}: {reasons}", file=sys.stderr)
        return 75
    if not shutil.which("systemd-run"):
        print("transcript analytics unavailable: systemd-run is required", file=sys.stderr)
        return 69
    command = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "-p",
        f"MemoryMax={ram}",
        "-p",
        "MemorySwapMax=0",
        "-p",
        f"CPUQuota={cpu:.2f}%",
        "-p",
        "RuntimeMaxSec=1h",
        "--",
        sys.executable,
        "-m",
        "genesis",
        *argv,
    ]
    try:
        return subprocess.run(
            command, env={**os.environ, _CHILD: f"{ram},{cpu:.2f}"}, check=False
        ).returncode
    except OSError as exc:
        print(f"transcript analytics unavailable: {exc}", file=sys.stderr)
        return 69
