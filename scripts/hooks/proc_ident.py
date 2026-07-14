"""Process-identity helpers for Claude Code hook scripts (stdlib-only).

Shared by duplicate_session_guard.py and genesis_session_context.py. The
server-side twin of this parse lives in
src/genesis/runtime/init/process_reaper.py (`_proc_age_secs`) — hooks cannot
import genesis.* (the package import tree would dominate a hook's latency
budget), so the ~15-line /proc/<pid>/stat parse is deliberately duplicated
between the two worlds. Keep the parsing rules in sync.

A process identity is the pair ``(pid, starttime)``: starttime is field 22 of
/proc/<pid>/stat (in clock ticks since boot) and never changes for a live
process, so a matching pair proves the pid was not recycled.
"""

from __future__ import annotations

import hashlib

# Nearest-claude-ancestor walk depth; matches session_activity_touch.sh.
_MAX_WALK = 20


def proc_stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat AFTER the comm field, or None.

    The comm field (2) is parenthesised and may contain spaces or ')', so
    split after the LAST ')': the remainder starts at field 3 (state).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("ascii", "replace")
    except OSError:
        return None
    _, _, after = raw.rpartition(") ")
    fields = after.split()
    return fields or None


def read_starttime(pid: int) -> int | None:
    """starttime (clock ticks since boot) for pid, or None if unreadable.

    Overall stat field 22 == index 19 of the post-comm remainder (state is
    field 3 / index 0). Same arithmetic as process_reaper._proc_age_secs.
    """
    fields = proc_stat_fields(pid)
    if fields is None or len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def read_ppid(pid: int) -> int | None:
    """Parent pid (overall stat field 4 == remainder index 1), or None."""
    fields = proc_stat_fields(pid)
    if fields is None or len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def read_comm(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return None


def find_claude_ancestor(start_pid: int) -> int | None:
    """Nearest ancestor (including start_pid) whose comm is exactly 'claude'.

    Mirrors session_activity_touch.sh's walk: nearest-first, so a nested
    `claude -p` spawned from a session's Bash tool attributes to the INNER
    claude — the correct owner for its own transcript.
    """
    pid = start_pid
    for _ in range(_MAX_WALK):
        if pid is None or pid <= 1:
            return None
        if read_comm(pid) == "claude":
            return pid
        pid = read_ppid(pid)
    return None


def is_alive(pid: int, starttime: int) -> bool:
    """True iff pid exists AND its starttime matches (guards pid reuse)."""
    current = read_starttime(pid)
    return current is not None and current == starttime


def transcript_key(transcript_path: str) -> str:
    """Stable registry key for a transcript path (16 hex chars)."""
    return hashlib.sha256(transcript_path.encode("utf-8")).hexdigest()[:16]
