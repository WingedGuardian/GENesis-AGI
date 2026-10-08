"""Per-process disk I/O from ``/proc/<pid>/io`` — who is doing the I/O right now.

STDLIB-ONLY, deliberately: the host-side Guardian imports this through
``genesis.guardian.cgroup_ops``, and its venv carries ``pyyaml`` and nothing
else (see ``genesis/util/host_boot.py`` for the same constraint).

Two callers, two pid sources, one sampler:
  - the Guardian (host side) ranks the pids in the container's cgroup during
    recovery (``cgroup_ops.find_top_io_pids_rate``);
  - the container watchdog ranks every pid it can see when its I/O pressure
    check fires, so a stall is logged with the processes behind it.

``/proc/<pid>/io`` is readable only for processes the caller may ptrace-read,
so processes owned by another user are unreadable. That is reported as a
count, never hidden: MEASURED 2026-10-07 inside the watchdog's own systemd
sandbox on a live container, 263 of 297 processes were readable and 34 denied.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

# Module-level so tests can repoint it at a fake tree (the host_boot.py seam).
_PROC = "/proc"


def read_proc_io(pid: int) -> dict | None:
    """Cumulative read/write bytes and ``comm`` for one pid, or None.

    None when the pid is gone or its ``io`` file is unreadable.
    """
    try:
        io_path = Path(_PROC) / str(pid) / "io"
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
            comm = (Path(_PROC) / str(pid) / "comm").read_text().strip()

        return {
            "pid": pid,
            "read_bytes": read_bytes,
            "write_bytes": write_bytes,
            "total_bytes": read_bytes + write_bytes,
            "comm": comm,
        }
    except (OSError, ValueError):
        return None


def rank_by_io_rate(
    pids: list[int],
    top_n: int = 5,
    sample_interval_s: float = 0.5,
) -> tuple[list[dict], int, float]:
    """Rank ``pids`` by current I/O rate, from two samples ``sample_interval_s`` apart.

    Returns ``(rates, readable, total_rate)``: the top ``top_n`` entries (keys
    pid, comm, read_rate, write_rate, total_rate in bytes/s,
    read_bytes_cumulative, write_bytes_cumulative); how many of ``pids`` had a
    readable first sample; and the summed rate over ALL readable pids, not just
    the top ones. The total is the denominator a reader needs: a stall with a
    small total was caused by something this caller cannot see.
    A pid that exits between samples is dropped.
    """
    t0: dict[int, dict] = {}
    for pid in pids:
        data = read_proc_io(pid)
        if data:
            t0[pid] = data

    if not t0:
        return [], 0, 0.0

    time.sleep(sample_interval_s)

    rates: list[dict] = []
    for pid, before in t0.items():
        after = read_proc_io(pid)
        if after is None:
            continue  # exited between samples
        delta_read = max(0, after["read_bytes"] - before["read_bytes"])
        delta_write = max(0, after["write_bytes"] - before["write_bytes"])
        rates.append(
            {
                "pid": pid,
                "comm": after["comm"],
                "read_rate": delta_read / sample_interval_s,
                "write_rate": delta_write / sample_interval_s,
                "total_rate": (delta_read + delta_write) / sample_interval_s,
                "read_bytes_cumulative": after["read_bytes"],
                "write_bytes_cumulative": after["write_bytes"],
            }
        )

    rates.sort(key=lambda x: x["total_rate"], reverse=True)
    return rates[:top_n], len(t0), sum(r["total_rate"] for r in rates)


def systemd_unit(pid: int) -> str | None:
    """The last component of the pid's cgroup path (its unit or scope), or None."""
    try:
        text = (Path(_PROC) / str(pid) / "cgroup").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            path = line[3:].strip().rstrip("/")
            return path.rsplit("/", 1)[-1] or None
    return None


def claude_ancestor(pid: int, max_depth: int = 16) -> int | None:
    """The pid of the nearest ancestor whose comm is ``claude``, or None.

    A process in a Claude Code session's scope is gone by the time anyone reads
    the log; the session's own pid resolves through ``cc_sessions.pid``. Reads
    only ``/proc/<pid>/stat`` (ppid) and ``comm``, never argv.
    """
    current = pid
    for _ in range(max_depth):
        try:
            stat = (Path(_PROC) / str(current) / "stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if ppid <= 1:
            return None
        try:
            comm = (Path(_PROC) / str(ppid) / "comm").read_text().strip()
        except OSError:
            return None
        if comm == "claude":
            return ppid
        current = ppid
    return None
