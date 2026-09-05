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
(``enumerate_spawn_slots``, world-readable) and identifies interactive sessions by cmdline
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

# Thresholds on the slot's WHOLE PROCESS TREE (MB) — see `slot_tree_rss_mb`.
# They were previously applied to the main `claude` process alone, which is
# about a third of a slot's real cost, so they could not fire in the regime
# they exist for: a session whose tree had ballooned to 6 GB still reported a
# ~0.8 GB `claude` process and read healthy.
#
# Rebased on a MEASURED per-slot DISTRIBUTION, not a mean — the mean of this
# population is nobody's slot. 2026-09-05, 7 concurrent sessions, whole-tree MB
# (two same-day measurements agreeing within drift): ~970/1030/1270 for idle
# slots, ~3100-3470 for working ones — bimodal, idle ~1.0 GB vs working
# ~3.1-3.5 GB. The threshold gates a per-slot MAX, so it is sized off the
# observed healthy MAX (~3.5 GB): WARN 6144 = ~1.8x that max, CRIT 8192 =
# ~2.4x. The old multiple is deliberately NOT preserved — 5x the healthy max
# would be ~17 GB, past the point where a human should already have looked.
# Tunable, and still deliberately coarse: this is leak DETECTION, not OOM
# prevention. Revisit once a week of dashboard data lands under the new
# denominator, since no history exists at this scale.
SLOT_RSS_WARN_MB = 6144
SLOT_RSS_CRIT_MB = 8192


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


def _read_ppid(pid: int) -> int | None:
    """Parent pid from ``/proc/<pid>/stat``, or None if gone/unreadable.

    Same parse discipline as ``read_proc_start_iso``: ``comm`` (field 2) is
    parenthesised and may itself contain spaces or ``)``, so the line is split
    after the LAST ``)``. Everything after that starts at field 3 (state), which
    puts ppid (field 4) at index 1. ``stat`` is world-readable, so unlike
    ``environ`` this works inside genesis-server's sandbox.
    """
    try:
        with open(f"{_PROC}/{pid}/stat") as f:
            after = f.read().rsplit(")", 1)[1].split()
        return int(after[1])
    except (OSError, ValueError, IndexError):
        return None


def build_child_map(pids: list[int]) -> dict[int, list[int]]:
    """``{ppid: [child pid, ...]}`` over the given pids.

    Built ONCE per enumeration and shared by every slot, so the /proc scan stays
    O(processes) rather than O(slots x processes). Pids that vanish mid-walk are
    simply absent — a dead child contributes no memory anyway. The asymmetric
    case is a child spawned AFTER the /proc listing: it DOES hold memory but is
    invisible until the next enumeration, so a brand-new session under-reports
    for one tick. Self-correcting; stated so nobody reads one low tick as truth.
    """
    children: dict[int, list[int]] = {}
    for pid in pids:
        ppid = _read_ppid(pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    return children


def slot_tree_rss_mb(pid: int, children: dict[int, list[int]]) -> float:
    """Summed RSS (MB) of ``pid`` and every descendant.

    A CC slot is not one process: each session also carries a Serena LSP server
    and a fleet of ``genesis_mcp_server.py`` children that live for the life of
    the session and, MEASURED, account for roughly two thirds of the slot's
    memory. Counting only the root understates a slot ~3x.

    RSS double-counts pages shared between parent and child, so this is an upper
    bound rather than a proportional-set measure. That is the deliberate choice:
    PSS via ``smaps_rollup`` is more accurate but costs a much larger read per
    process, and for THRESHOLD purposes over-counting shared pages fails toward
    looking, which is the safe direction for a leak detector. MEASURED
    2026-09-05 (adversarial review, summed smaps_rollup Pss per tree): the
    inflation is ~1.15x on heavy slots and 1.5-1.8x on idle ones — inversely
    correlated with size, since a leaking slot's growth is private memory. So
    near the threshold the over-count is ~15%, not a false-alarm generator.

    Cycle-safe (a malformed ppid chain cannot loop forever) via an explicit
    seen-set; unreadable descendants are skipped, never fatal.
    """
    total = 0.0
    seen: set[int] = set()
    stack = [pid]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        rss = read_proc_rss_mb(cur)
        if rss is not None:
            total += rss
        stack.extend(children.get(cur, ()))
    return round(total, 1)


def slot_status(rss_mb: float) -> str:
    """Map a slot's TREE RSS (MB) to a health status."""
    if rss_mb >= SLOT_RSS_CRIT_MB:
        return "error"
    if rss_mb >= SLOT_RSS_WARN_MB:
        return "degraded"
    return "healthy"


def enumerate_cc_slots() -> list[dict]:
    """One row per live CC session: ``{slot, pid, rss_mb, proc_rss_mb, status,
    started_at}``.

    ``rss_mb`` is the slot's WHOLE PROCESS TREE (the `claude` process plus its
    Serena and MCP children — see ``slot_tree_rss_mb``), which is what a slot
    actually costs; ``proc_rss_mb`` is the root `claude` process alone, which is
    what this function used to report as ``rss_mb``.

    Walks /proc for ``claude`` processes. A process is kept only if it is an
    INTERACTIVE session (``_is_interactive`` — not a headless ``claude -p``
    cognitive/background call), so Genesis's own internal LLM subprocesses (which
    share ``comm=="claude"``) don't masquerade as sessions; cmdline is the
    authoritative signal, checked before any slot label is trusted. The slot label
    is then enrichment: from the process's environ (``GENESIS_SLOT``) when
    readable, else from the ``mcp-spawn`` file plane (``enumerate_spawn_slots``) — the latter
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
    # sandbox-safe slot source. Map pid -> (slot, spawn_at) so a recycled pid can
    # be rejected below by comparing spawn_at to the live process's start time.
    spawn_by_pid: dict[int, tuple[str, str]] = {}
    _rec_current = None
    try:
        from genesis.observability.mcp_spawn_store import (
            enumerate_spawn_slots,
        )
        from genesis.observability.mcp_spawn_store import (
            spawn_record_is_current as _rec_current,
        )

        spawn_by_pid = {pid: (slot, sat) for slot, pid, sat in enumerate_spawn_slots()}
    except Exception:
        logger.debug("cc_slots: spawn-plane lookup failed", exc_info=True)

    # One /proc scan for the whole enumeration (see `build_child_map`).
    child_map = build_child_map(pids)

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
        rss = read_proc_rss_mb(pid)
        if rss is None:
            continue
        # `rss_mb` is the WHOLE TREE, deliberately. The alert path reads that key
        # directly (awareness/loop.py), so leaving it as the root process would
        # have kept every existing consumer on the understated number while only
        # a new key told the truth. Making the established name honest means a
        # consumer that is never updated is right by default; the root-process
        # figure stays available as `proc_rss_mb` for anyone who needs it.
        tree_rss = slot_tree_rss_mb(pid, child_map)
        started_at = read_proc_start_iso(pid)
        # Slot label: environ (unsandboxed) first, else the spawn-file plane — but
        # only when the plane record's pid was NOT recycled (its spawn_at is
        # consistent with this process's start time); otherwise leave it unlabeled.
        plane = spawn_by_pid.get(pid)
        plane_slot = (
            plane[0] if plane and _rec_current and _rec_current(plane[1], started_at) else None
        )
        slot = _slot_label(pid) or plane_slot
        rows.append(
            {
                "slot": slot,
                "pid": pid,
                "rss_mb": tree_rss,
                "proc_rss_mb": rss,
                "status": slot_status(tree_rss),
                "started_at": started_at,
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
