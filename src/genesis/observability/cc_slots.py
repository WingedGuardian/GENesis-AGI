"""Per-CC-slot memory (RSS) enumeration for leak/anomaly detection.

Genesis runs concurrent Claude Code sessions ("slots"); each is a `claude`
process carrying ``GENESIS_SLOT=<n>`` in its environ, normally ~0.7-1.0 GB RSS.
A single session that balloons (a leak) is otherwise invisible — this surfaces
per-slot RSS so it can be shown on the dashboard and alerted on. This is leak
DETECTION, not OOM prevention (the container has ample headroom).

Stdlib-only leaf module (no genesis imports → no import cycle). Uses same-uid
/proc reads, which succeed despite yama ``ptrace_scope=1`` (that gates
PTRACE_ATTACH, not the READ mode used for ``/proc/<pid>/environ``).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROC = "/proc"

# Thresholds on the main `claude` process RSS (MB). Normal is ~0.7-1.0 GB, so
# 4 GB WARN is ~4x baseline. Tunable — revisit once a week of dashboard data
# lands (a very large long-lived session can legitimately reach 2-3 GB).
SLOT_RSS_WARN_MB = 4096
SLOT_RSS_CRIT_MB = 6144


def read_proc_rss_mb(pid: int) -> float | None:
    """VmRSS of a pid in MB, or None if the pid is gone/unreadable."""
    try:
        with open(f"{_PROC}/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)  # kB → MB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _slot_label(pid: int) -> str | None:
    """The GENESIS_SLOT value from a pid's environ, or None if absent/unreadable."""
    try:
        with open(f"{_PROC}/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    for entry in raw.split(b"\x00"):
        if entry.startswith(b"GENESIS_SLOT="):
            val = entry.partition(b"=")[2].decode("utf-8", "replace").strip()
            return val or None
    return None


def slot_status(rss_mb: float) -> str:
    """Map a slot's RSS (MB) to a health status."""
    if rss_mb >= SLOT_RSS_CRIT_MB:
        return "error"
    if rss_mb >= SLOT_RSS_WARN_MB:
        return "degraded"
    return "healthy"


def enumerate_cc_slots() -> list[dict]:
    """One row per CC slot: ``{slot, pid, rss_mb, status}``, sorted by slot.

    Walks /proc for `claude` processes tagged with GENESIS_SLOT and reads each
    one's VmRSS. If two `claude` processes somehow share a slot, the larger RSS
    wins. Best-effort: returns [] on failure (logged at DEBUG so an empty list
    caused by an error is distinguishable in the logs from a genuine no-slots
    state — this is a visibility feature, so a silent hole would defeat it).
    """
    try:
        pids = [int(n) for n in os.listdir(_PROC) if n.isdigit()]
    except OSError:
        logger.debug("cc_slots: cannot list /proc", exc_info=True)
        return []

    by_slot: dict[str, dict] = {}
    for pid in pids:
        try:
            with open(f"{_PROC}/{pid}/comm") as f:
                if f.read().strip() != "claude":
                    continue
        except OSError:
            continue  # pid vanished or unreadable — normal, skip
        label = _slot_label(pid)
        if label is None:
            continue
        rss = read_proc_rss_mb(pid)
        if rss is None:
            continue
        prev = by_slot.get(label)
        if prev is None or rss > prev["rss_mb"]:
            by_slot[label] = {"slot": label, "pid": pid, "rss_mb": rss, "status": slot_status(rss)}
    # Numeric-aware sort so "10" follows "9" (labels are numeric strings today);
    # any non-numeric label sorts last.
    return sorted(
        by_slot.values(),
        key=lambda r: (0, int(r["slot"])) if r["slot"].isdigit() else (1, r["slot"]),
    )
