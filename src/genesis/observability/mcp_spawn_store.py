"""Cross-process handoff of a CC session's MCP-subprocess spawn commit.

Each session's FastMCP stdio subprocesses capture their spawn commit in memory
(``mcp_spawn_identity``), but the dashboard that renders the stale-code badge
runs in a DIFFERENT process (genesis-server) and cannot read that memory. This
leaf persists ``<session_pid> <commit>`` per slot to a small file plane so the
dashboard can read it — mirroring the ``~/.genesis/mcp_crashes/`` precedent.

Stdlib-only leaf (no genesis imports → no import cycle; dashboard-safe, never
pulls in ``fastmcp``). The file is keyed by ``GENESIS_SLOT`` and validated by the
recorded session pid on read, so a slot reused by a new session that hasn't
rewritten the file yet reads as UNKNOWN (fail-open), never a false badge.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SPAWN_DIR = Path.home() / ".genesis" / "mcp-spawn"
_PROC = "/proc"


def _slot_path(slot: str) -> Path:
    return _SPAWN_DIR / slot


def session_pid() -> int | None:
    """The claude session pid for this MCP subprocess, or None.

    ``.claude/mcp/run-mcp-server`` ends in ``exec python …``, so the python MCP
    process REPLACES the wrapper and its parent is ``claude`` directly — the
    pid the dashboard enumerates per slot. Verified via ``comm`` so a headless/
    atypical launch (parent not ``claude``) yields None → nothing persisted.
    """
    ppid = os.getppid()
    try:
        with open(f"{_PROC}/{ppid}/comm") as f:
            if f.read().strip() == "claude":
                return ppid
    except OSError:
        return None
    return None


def persist_spawn_commit(
    slot: str, pid: int | None, commit: str | None, spawn_at: str | None
) -> None:
    """Best-effort ATOMIC write of ``<pid> <commit> <spawn_at>`` for a slot.

    ``spawn_at`` (ISO-8601, no spaces) is stored alongside the commit so the
    dashboard can apply the SAME identity-AND-time staleness verdict as Part A's
    guard (a session AHEAD of the last recorded deploy must not be flagged).

    Never raises — this is telemetry and must not perturb the caller (Part A's
    guard capture). All five of a session's MCP servers call this with the same
    (claude pid, commit, spawn_at), so concurrent writes are identical; the
    temp-file + ``os.replace`` makes each write atomic (no torn reads).
    """
    if not slot or not commit or pid is None or not spawn_at:
        return
    try:
        _SPAWN_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_SPAWN_DIR), prefix=f".{slot}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(f"{pid} {commit} {spawn_at}\n")
            os.replace(tmp, str(_slot_path(slot)))  # atomic same-dir rename
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception:
        logger.debug("mcp_spawn_store: persist failed for slot %s", slot, exc_info=True)


def enumerate_spawn_slots() -> list[tuple[str, int, str]]:
    """``(slot_label, recorded_pid, spawn_at)`` for each persisted spawn file.

    These are world-readable regular files (no ptrace-gated read), so this works
    from the sandboxed genesis-server where ``/proc/<pid>/environ`` is unreadable —
    it is how an in-server enumerator recovers a live session's slot without the
    (sandbox-blocked) environ. ``spawn_at`` is returned so the caller can reject a
    record whose recorded pid was RECYCLED by a newer process (see
    ``spawn_record_is_current``). Best-effort; never raises. Skips the atomic-write
    temp files (``mkstemp`` names them ``.<slot>.<rand>`` — dot-prefixed) and any
    torn/malformed file.
    """
    out: list[tuple[str, int, str]] = []
    try:
        names = os.listdir(_SPAWN_DIR)
    except OSError:
        return out
    for name in names:
        if name.startswith("."):  # atomic-write temp file, not a slot
            continue
        try:
            raw = (_SPAWN_DIR / name).read_text().strip()
        except OSError:
            continue
        parts = raw.split()
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        out.append((name, pid, parts[2]))
    return out


def spawn_record_is_current(
    spawn_at: str | None, proc_start_iso: str | None, grace_s: float = 120.0
) -> bool:
    """Whether a live process's start time is consistent with a spawn record.

    A spawn record maps a pid → slot/commit, but the OS can RECYCLE that pid for a
    different process once the recorded session exits (before disk-hygiene prunes
    the dead-pid file). The record is about the recorded session only if the live
    process started no LATER than the record was written (``spawn_at`` is captured
    at MCP-subprocess start, which is a beat after the claude process itself
    started, so a genuine match has ``proc_start <= spawn_at``); a recycled pid
    belongs to a process that started well AFTER the stale ``spawn_at``. A small
    grace absorbs clock skew. Fail-OPEN (True) when either timestamp is missing or
    unparseable — the pid match stays the primary signal and a momentarily
    unreadable ``/proc/<pid>/stat`` must not drop a valid record.
    """
    if not spawn_at or not proc_start_iso:
        return True
    try:
        ps = datetime.fromisoformat(proc_start_iso)
        sa = datetime.fromisoformat(spawn_at)
    except (ValueError, TypeError):
        return True
    return (ps - sa).total_seconds() <= grace_s


def read_spawn_identity(
    slot: str, live_pid: int | None, proc_start_iso: str | None = None
) -> tuple[str, str] | None:
    """``(commit, spawn_at)`` this slot's CURRENT session was spawned at, or None.

    Returns the pair ONLY if the file's recorded pid == ``live_pid`` AND — when
    ``proc_start_iso`` is supplied — the live process's start time is consistent
    with the recorded ``spawn_at`` (``spawn_record_is_current``), so a pid RECYCLED
    by a new session after the old one exited is not mistaken for the recorded one.
    A slot reused by a new session whose MCP servers have not rewritten the file
    yet → pid mismatch (or start-time mismatch) → None (fail-open, no false badge
    during the brief startup window). Missing or malformed file → None.
    """
    if not slot or live_pid is None:
        return None
    try:
        raw = _slot_path(slot).read_text().strip()
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 3:
        return None
    pid_str, commit, spawn_at = parts
    try:
        recorded_pid = int(pid_str)
    except ValueError:
        return None
    if recorded_pid != live_pid or not commit or not spawn_at:
        return None
    if not spawn_record_is_current(spawn_at, proc_start_iso):
        return None
    return commit, spawn_at
