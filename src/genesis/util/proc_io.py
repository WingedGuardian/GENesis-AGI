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
from collections.abc import Callable, Iterable
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
            # Bytes, not text: a process may set its comm to any non-NUL bytes,
            # and a decode error must not drop a pid whose counters were read.
            raw = (Path(_PROC) / str(pid) / "comm").read_bytes()
            comm = raw.decode("utf-8", "replace").strip() or "unknown"

        return {
            "pid": pid,
            "read_bytes": read_bytes,
            "write_bytes": write_bytes,
            "total_bytes": read_bytes + write_bytes,
            "comm": comm,
        }
    except (OSError, ValueError):
        return None


def read_starttime(pid: int) -> int | None:
    """The pid's start time (field 22 of ``/proc/<pid>/stat``, in clock ticks), or None.

    A pid number is reused once its process exits; ``(pid, starttime)`` names one
    process. ``comm`` (field 2) may hold spaces and ``)``, so fields are counted
    from after the LAST ``)``: there field 3 is index 0, and field 22 index 19.
    """
    try:
        stat = (Path(_PROC) / str(pid) / "stat").read_text()
        return int(stat[stat.rindex(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def rank_by_io_rate(
    pids: list[int],
    top_n: int = 5,
    sample_interval_s: float = 0.5,
    *,
    pid_source: Callable[[], Iterable[int]] | None = None,
) -> tuple[list[dict], int, float, dict]:
    """Rank ``pids`` by current I/O rate, from two samples ``sample_interval_s`` apart.

    Returns ``(rates, readable, total_rate, churn)``: the top ``top_n`` entries
    (keys pid, comm, read_rate, write_rate, total_rate in bytes/s,
    read_bytes_cumulative, write_bytes_cumulative); how many of ``pids`` had a
    readable first sample; the summed rate over ALL measured pids, not just the
    top ones (the denominator a reader needs: a stall with a small total was
    caused by something this caller cannot see); and ``churn`` =
    ``{"exited": n, "started": n}``.

    Processes come and go during the interval, and the culprit of a stall is
    often a short-lived one, so neither kind is silently dropped:
      - ``exited``: readable at the first sample, gone at the second. Its I/O in
        the interval cannot be read, so it is COUNTED and reported, never
        presented as "no I/O";
      - ``started``: with ``pid_source`` (re-listed after the interval), a pid
        absent from ``pids`` is measured from zero, since all its I/O happened
        within the interval;
      - a pid REUSED within the interval: the two samples are matched by
        ``(pid, starttime)``, never by pid number alone, so a delta is never
        taken between two different processes. A changed start time counts the
        first process ``exited`` and the second ``started`` (measured from zero,
        as above). When the start time is readable at one sample and not the
        other, identity is unknown: the pid counts as ``exited`` and is not
        ranked, since neither a delta nor a from-zero measure is trustworthy and
        the Guardian acts on this ranking. Unreadable at both is treated as the
        same process (``stat`` is world-readable, so that is a gone or fake pid).
    A rate divides by the time between that pid's two reads, measured, since a
    sleep under heavy I/O pressure overruns its request. ``time.sleep`` never
    returns early (PEP 475 retries it after a signal), so the measured time is
    never below the request; the ``max`` only keeps a stubbed sleep meaningful.
    """
    churn = {"exited": 0, "started": 0}
    t0: dict[int, tuple[dict, float, int | None]] = {}
    for pid in pids:
        # Start time BEFORE the counters here (after them at the second read):
        # a pid reused between the two reads then shows as a changed start time
        # instead of pairing one process's counters with the next one's identity.
        started_at = read_starttime(pid)
        data = read_proc_io(pid)
        if data:
            t0[pid] = (data, time.monotonic(), started_at)

    if not t0 and pid_source is None:
        return [], 0, 0.0, churn

    begun = time.monotonic()
    time.sleep(sample_interval_s)

    def _rate(pid: int, after: dict, before: dict | None, since: float) -> dict:
        elapsed = max(time.monotonic() - since, sample_interval_s)
        delta_read = max(0, after["read_bytes"] - (before["read_bytes"] if before else 0))
        delta_write = max(0, after["write_bytes"] - (before["write_bytes"] if before else 0))
        return {
            "pid": pid,
            "comm": after["comm"],
            "read_rate": delta_read / elapsed,
            "write_rate": delta_write / elapsed,
            "total_rate": (delta_read + delta_write) / elapsed,
            "read_bytes_cumulative": after["read_bytes"],
            "write_bytes_cumulative": after["write_bytes"],
        }

    rates: list[dict] = []
    for pid, (before, read_at, started_at) in t0.items():
        after = read_proc_io(pid)
        if after is None:
            churn["exited"] += 1  # its I/O in the interval is unreadable, not zero
            continue
        now_started_at = read_starttime(pid)
        if now_started_at == started_at:
            rates.append(_rate(pid, after, before, read_at))
            continue
        churn["exited"] += 1  # the first process is gone; its pid was reused
        if started_at is not None and now_started_at is not None:
            churn["started"] += 1  # a new process, all of its I/O within the interval
            rates.append(_rate(pid, after, None, begun))
    if pid_source is not None:
        known = set(pids)
        for pid in pid_source():
            if pid in known:
                continue
            after = read_proc_io(pid)
            if after is not None:
                churn["started"] += 1
                rates.append(_rate(pid, after, None, begun))

    rates.sort(key=lambda x: x["total_rate"], reverse=True)
    return rates[:top_n], len(t0), sum(r["total_rate"] for r in rates), churn


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
