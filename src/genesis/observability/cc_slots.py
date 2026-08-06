"""Per-CC-slot memory (RSS) enumeration for leak/anomaly detection.

Genesis runs concurrent Claude Code sessions ("slots"); each is a `claude`
process carrying ``GENESIS_SLOT=<n>`` in its environ, normally ~0.7-1.0 GB RSS.
A single session that balloons (a leak) is otherwise invisible — this surfaces
per-slot RSS so it can be shown on the dashboard and alerted on. This is leak
DETECTION, not OOM prevention (the container has ample headroom).

Stdlib-only leaf at import time (the one genesis import — ``mcp_spawn_store``,
itself a stdlib-only leaf — is lazy, inside the function, so no import cycle).

IMPORTANT — reading ``/proc/<pid>/environ`` of ANOTHER process is ptrace-FSCREDS
gated and FAILS with EACCES inside genesis-server's systemd sandbox
(``ProtectSystem=strict``); it only succeeds from an unsandboxed shell. So the
slot label (``GENESIS_SLOT``, environ-only) is NOT readable in-server. This
module therefore enriches the slot label from the ``mcp-spawn`` file plane
(``slot_by_pid``, world-readable) and identifies interactive sessions by cmdline
(``/proc/<pid>/cmdline``, world-readable) — neither is ptrace-gated, so both work
under the sandbox. VmRSS/comm/stat are likewise world-readable. See the
``verify_real_runtime_context`` lesson (shipped-green-but-inert since #855).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

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


def _read_btime() -> int | None:
    """Boot time (epoch seconds) from ``/proc/stat``'s ``btime`` line, or None.

    Read fresh each call (not cached) so tests that repoint ``_PROC`` see their
    own fake; the caller reads it at most once per slot on a cold dashboard
    fetch, so the cost is trivial.
    """
    try:
        with open(f"{_PROC}/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def read_proc_start_iso(pid: int) -> str | None:
    """Wall-clock start time of a pid as an ISO-8601 UTC string, or None.

    Computed as ``btime + starttime_ticks / CLK_TCK`` where ``starttime`` is
    field 22 of ``/proc/<pid>/stat`` (clock ticks since boot). The ``comm``
    field (2) is parenthesised and may itself contain spaces or ``)``, so the
    line is split after the LAST ``)`` — everything after that is whitespace-
    delimited starting at field 3 (state), making ``starttime`` index 19.
    Verified to-the-second against ``ps lstart`` on this host. Returns None on
    any read/parse failure or an implausible clock tick (fail-open: the caller
    treats an unknown start time as "not stale", never falsely flagging).
    """
    btime = _read_btime()
    if btime is None:
        return None
    try:
        with open(f"{_PROC}/{pid}/stat") as f:
            after = f.read().rsplit(")", 1)[1].split()
        starttime_ticks = int(after[19])
        clk = os.sysconf("SC_CLK_TCK")
        if clk <= 0:
            return None
        wall = btime + starttime_ticks / clk
        return datetime.fromtimestamp(wall, UTC).isoformat()
    except (OSError, ValueError, IndexError, OverflowError):
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


def _is_interactive(pid: int) -> bool:
    """True if a ``claude`` pid is an INTERACTIVE session, not a headless
    ``claude -p`` cognitive/background call.

    Genesis spawns internal ``claude -p`` subprocesses (CCInvoker triage/reflection/
    extraction, the dashboard update router, the experiment router) that share
    ``comm=="claude"`` but are NOT interactive slots. Interactive sessions run
    WITHOUT ``-p``/``--print``; every headless invocation uses it (verified:
    ``cc/invoker.py``, ``dashboard/routes/updates.py``, ``experimentation/
    cc_router.py``). ``/proc/<pid>/cmdline`` is world-readable, so this
    discriminates even inside the server sandbox (where environ is not readable).
    Exact-arg match (NUL-separated argv) avoids matching ``-p`` inside a value or
    another flag. Unreadable cmdline → treated as non-interactive (conservative:
    never let an internal cognitive call masquerade as a session)."""
    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\x00")
    except OSError:
        return False
    return b"-p" not in args and b"--print" not in args


def slot_status(rss_mb: float) -> str:
    """Map a slot's RSS (MB) to a health status."""
    if rss_mb >= SLOT_RSS_CRIT_MB:
        return "error"
    if rss_mb >= SLOT_RSS_WARN_MB:
        return "degraded"
    return "healthy"


def enumerate_cc_slots() -> list[dict]:
    """One row per live CC session: ``{slot, pid, rss_mb, status, started_at}``.

    Walks /proc for ``claude`` processes. A process is kept only if it is an
    INTERACTIVE session (``_is_interactive`` — not a headless ``claude -p``
    cognitive/background call), so Genesis's own internal LLM subprocesses (which
    share ``comm=="claude"``) don't masquerade as sessions; cmdline is the
    authoritative signal, checked before any slot label is trusted. The slot label
    is then enrichment: from the process's environ (``GENESIS_SLOT``) when
    readable, else from the ``mcp-spawn`` file plane (``slot_by_pid``) — the latter
    is the ONLY source that survives genesis-server's ``ProtectSystem`` sandbox,
    where ``/proc/<pid>/environ`` reads return EACCES.

    Rows are keyed by pid (each live claude is one row); ``rss_mb``/``started_at``
    come from world-readable /proc. ``slot`` may be None for an unregistered
    interactive session (launched before the spawn-store writer existed, or a
    manual ``claude``) — the dashboard join is pid-based so such rows still join,
    and they are never flagged stale (no persisted commit). ``started_at`` is the
    wall-clock start (ISO UTC, None if unreadable), a faithful proxy for the
    session's MCP-subprocess code version. Best-effort: [] on failure (logged at
    DEBUG so an error-empty is distinguishable from a genuine no-slots state).
    """
    try:
        pids = [int(n) for n in os.listdir(_PROC) if n.isdigit()]
    except OSError:
        logger.debug("cc_slots: cannot list /proc", exc_info=True)
        return []

    # Lazy import keeps this module a stdlib-only leaf at import time (no cycle —
    # mcp_spawn_store imports nothing from genesis). The file-plane read is the
    # sandbox-safe slot source.
    try:
        from genesis.observability.mcp_spawn_store import slot_by_pid

        spawn_slots = slot_by_pid()
    except Exception:
        logger.debug("cc_slots: spawn-plane lookup failed", exc_info=True)
        spawn_slots = {}

    rows: list[dict] = []
    for pid in pids:
        try:
            with open(f"{_PROC}/{pid}/comm") as f:
                if f.read().strip() != "claude":
                    continue
        except OSError:
            continue  # pid vanished or unreadable — normal, skip
        # cmdline is the AUTHORITATIVE interactive/headless signal, checked BEFORE
        # any slot label is trusted: a headless `claude -p` cognitive/background
        # call is never a session, even if a stale spawn-file entry maps its
        # (possibly OS-reused) pid to a slot. The slot label is enrichment only.
        if not _is_interactive(pid):
            continue
        slot = _slot_label(pid) or spawn_slots.get(pid)
        rss = read_proc_rss_mb(pid)
        if rss is None:
            continue
        rows.append(
            {
                "slot": slot,
                "pid": pid,
                "rss_mb": rss,
                "status": slot_status(rss),
                "started_at": read_proc_start_iso(pid),
            }
        )

    # Numeric-aware sort: digit-labeled slots first (so "10" follows "9"), then
    # non-numeric labels, then unlabeled (None) rows by pid. All sort keys are
    # (int-tag, str) so no int/str comparison across classes.
    def _sort_key(r: dict) -> tuple[int, str]:
        s = r["slot"]
        if s is None:
            return (2, f"{r['pid']:020d}")
        if s.isdigit():
            return (0, f"{int(s):020d}")
        return (1, s)

    return sorted(rows, key=_sort_key)
