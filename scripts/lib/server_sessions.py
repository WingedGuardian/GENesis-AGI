"""Which Claude Code sessions would a genesis-server restart end?

Usage: server_sessions.py <server_pid> [proc_root [caller_pid]]
       server_sessions.py --rows <db_path> <server_boot_unix>

The first form prints one line per live Claude Code process DESCENDED from
<server_pid> (a zombie, exited but not yet reaped, is not live):

    <pid>\\t<seconds running>\\t<resume session id, or ->\\t<self, or ->\\t<start>

and exits 0 (no lines = none). "self" marks a session that is an ancestor of
<caller_pid>: the session asking for the restart is one of those the restart
would end. <start> is the process's start time in clock ticks since host boot
(/proc/<pid>/stat field 22): a pid plus its start names one process even after
the pid is reused. Exits 2 when <proc_root> (default /proc) cannot be listed at
all, so the caller cannot tell; the caller decides what that means.

The second form prints one line per background session the CURRENT server boot
is still running, from the cc_sessions table:

    <session id, or ->\\t<source tag>\\t<started_at>

and exits 2 when the table cannot be read. A dispatched session or a reflection
holds its row from before its Claude process starts until after it exits (the
result is stored, audited and delivered), and a restart cancels it anywhere in
that span, so its row, not its process, is the session's lifetime. A row still
"active" from an EARLIER boot is a leftover (a clean stop marks dispatched work
failed; a reflection cancelled mid-run never closes its row), so only rows
started since <server_boot_unix> count. Telegram turns are not here: their rows
are long-lived conversations, and the process scan sees an in-flight turn.
Text is printed as one bounded printable line per field (other callers write
it, and whoever decides on the override reads it); a session id is printed only
when it is uuid-shaped, since the override names sessions by it.

Why descendants of the server, and not a marker or a table:

* The server runs each session it launches (dispatched work, a Telegram turn, a
  reflection) through ``systemd-run --user --scope``, which executes the command
  in place: the session keeps that pid, its parent is the server, and it sits in
  its own ``run-*.scope`` cgroup (MEASURED on systemd 255). So the server's own
  cgroup holds no session, while its process tree holds every one.
* A restart ends exactly those: on stop the server cancels its in-flight work, and
  every launcher kills its OWN child's process group on cancellation.
* The ``GENESIS_CC_SESSION=1`` environment stamp is NOT the same set: the ambient
  judges (``session_awareness/headless.py``) set it too, and they run under
  detached hook workers that a restart never touches. ``/proc/<pid>/environ`` is
  also ptrace-gated (``src/genesis/cc/slot_liveness.py``), so reading it would fail
  for exactly the processes that matter.
* The ``cc_sessions`` table cannot replace the scan: dispatched-task rows record
  no pid (0 of 144 on a live install) and reflection rows only sometimes (28 of
  83), while Telegram turns are stored as ``foreground``. MEASURED 2026-10-04.
  It complements it (the --rows form): a background session lives longer than
  its Claude process.

The claude-process rules below are a COPY of ``src/genesis/cc/slot_liveness.py``
(this file is stdlib-only and runs before the venv is trusted, so it cannot import
``genesis``). ``tests/test_scripts/test_server_sessions.py`` keeps the two in step.
"""

from __future__ import annotations

import os
import re
import sqlite3
import string
import sys
import time
import urllib.parse
from pathlib import Path

_CLAUDE_NAMES = frozenset({b"claude", b"claude.exe", b"claude-code"})
_INTERPRETERS = frozenset({b"node", b"nodejs", b"bun", b"deno"})
_ENTRY_SCRIPTS = frozenset({b"cli.js"})
# A process tree deeper than this is not a server's; the bound only stops a
# malformed /proc from looping.
_MAX_DEPTH = 64
_ID_CHARS = frozenset(string.hexdigits + "-")
# A process that has exited but not been reaped (Z), or is being torn down (X, x),
# has no work left to lose and cannot be killed.
_DEAD_STATES = frozenset({b"Z", b"X", b"x"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MAX_ROWS = 20


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _runs_entry_script(args: list[bytes]) -> bool:
    return any(a.rsplit(b"/", 1)[-1] in _ENTRY_SCRIPTS for a in args)


def _is_claude(comm: bytes | None, cmdline: bytes | None) -> bool:
    if comm is not None and comm.strip() in _CLAUDE_NAMES:
        return True
    if not cmdline:
        return False
    args = [a for a in cmdline.split(b"\x00") if a]
    if not args:
        return False
    base = args[0].rsplit(b"/", 1)[-1]
    if base in _CLAUDE_NAMES:
        return True
    return base in _INTERPRETERS and _runs_entry_script(args[1:])


def _stat_fields(raw: bytes) -> list[bytes] | None:
    """The fields of /proc/<pid>/stat AFTER the parenthesised comm (state first).

    The comm may itself contain spaces or ')', so the split starts at the LAST ')'.
    """
    try:
        return raw[raw.rindex(b")") + 1 :].split()
    except ValueError:
        return None


def _resume_id(cmdline: bytes | None) -> str:
    """The session id after --resume, or "-". Nothing else from the command line is
    reported: a dispatched session's prompt is on it."""
    if not cmdline:
        return "-"
    args = [a for a in cmdline.split(b"\x00") if a]
    for i, a in enumerate(args[:-1]):
        if a == b"--resume":
            value = args[i + 1].decode("ascii", "replace")
            # A session id is a uuid; anything else is not printed.
            if value and all(c in _ID_CHARS for c in value) and len(value) <= 64:
                return value
            return "-"
    return "-"


def server_sessions(
    server_pid: int, proc_root: Path, caller_pid: int | None = None
) -> list[tuple[int, int, str, bool, int]] | None:
    """``(pid, seconds running, resume id, is the caller's ancestor, start ticks)``
    for each live Claude Code process below *server_pid*, or None when
    *proc_root* cannot be listed."""
    try:
        entries = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return None
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        ticks = 100  # the Linux USER_HZ default
    # A process's start time counts clock ticks from HOST boot. /proc/uptime cannot
    # be the other operand: in an LXC container it is virtualized to the
    # container's own uptime (MEASURED: 704,086 s against a start time of
    # 1,553,845 s), so every age reads as zero. btime in /proc/stat is the host's
    # boot as a wall-clock time, which is what ps uses (agrees with ps to 1 s).
    btime: int | None = None
    raw_stat = _read(proc_root / "stat")
    if raw_stat:
        for line in raw_stat.splitlines():
            if line.startswith(b"btime "):
                try:
                    btime = int(line.split()[1])
                except (ValueError, IndexError):
                    btime = None
                break
    now = time.time()

    parent: dict[int, int] = {}
    started: dict[int, int] = {}
    claude: list[int] = []
    for entry in entries:
        pid = int(entry.name)
        raw = _read(entry / "stat")
        fields = _stat_fields(raw) if raw is not None else None
        if not fields or len(fields) < 20:
            # Gone between the listing and the read: it is not running, so a
            # restart cannot end it.
            continue
        try:
            parent[pid] = int(fields[1])
            started[pid] = int(fields[19])
        except ValueError:
            continue
        if fields[0] in _DEAD_STATES:
            continue
        if _is_claude(_read(entry / "comm"), _read(entry / "cmdline")):
            claude.append(pid)

    caller_chain: set[int] = set()
    cur, depth = caller_pid, 0
    while cur is not None and cur in parent and depth < _MAX_DEPTH:
        caller_chain.add(cur)
        cur = parent[cur]
        depth += 1

    found = []
    for pid in sorted(claude):
        cur, depth = pid, 0
        while cur in parent and depth < _MAX_DEPTH:
            cur = parent[cur]
            depth += 1
            if cur == server_pid:
                age = -1
                if btime is not None and ticks:
                    age = max(0, int(now - (btime + started[pid] / ticks)))
                found.append(
                    (
                        pid,
                        age,
                        _resume_id(_read(proc_root / str(pid) / "cmdline")),
                        pid in caller_chain,
                        started[pid],
                    )
                )
                break
            if cur <= 1:
                break
    return found


def _clean(value: object, limit: int) -> str:
    text = "".join(c if " " <= c <= "~" else "?" for c in str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def server_rows(db_path: str, boot_unix: int) -> list[tuple[str, str, str]] | None:
    """``(session id or "-", source tag, started_at)`` for each background session
    started since *boot_unix* and still active, or None when the table cannot be
    read. Sanitized for printing; capped (the caller is told how many more)."""
    # The table's started_at is ISO UTC with microseconds ("...T21:41:42.123456+00:00").
    since = time.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00", time.gmtime(boot_unix))
    try:
        con = sqlite3.connect(
            "file:" + urllib.parse.quote(db_path) + "?mode=ro", uri=True, timeout=2
        )
        try:
            rows = con.execute(
                "SELECT id, COALESCE(source_tag, ?), started_at FROM cc_sessions "
                "WHERE status = ? AND session_type IN (?, ?) AND started_at >= ? "
                "ORDER BY started_at",
                ("", "active", "background_task", "background_reflection", since),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    out = []
    for rid, tag, started in rows:
        rid_text = str(rid)
        out.append(
            (
                rid_text if _UUID.fullmatch(rid_text) else "-",
                _clean(tag, 60),
                _clean(started, 40),
            )
        )
    return out


def _rows_main(argv: list[str]) -> int:
    if len(argv) != 4 or not argv[3].isdigit():
        print("usage: server_sessions.py --rows <db_path> <server_boot_unix>", file=sys.stderr)
        return 2
    rows = server_rows(argv[2], int(argv[3]))
    if rows is None:
        print("cannot read the cc_sessions table", file=sys.stderr)
        return 2
    for rid, tag, started in rows[:_MAX_ROWS]:
        print(f"{rid}\t{tag}\t{started}")
    if len(rows) > _MAX_ROWS:
        print(f"more\t{len(rows) - _MAX_ROWS}\t-")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--rows":
        return _rows_main(argv)
    if (
        len(argv) not in (2, 3, 4)
        or not argv[1].isdigit()
        or (len(argv) == 4 and not argv[3].isdigit())
    ):
        print("usage: server_sessions.py <server_pid> [proc_root [caller_pid]]", file=sys.stderr)
        return 2
    proc_root = Path(argv[2]) if len(argv) >= 3 else Path("/proc")
    caller = int(argv[3]) if len(argv) == 4 else None
    found = server_sessions(int(argv[1]), proc_root, caller)
    if found is None:
        print(f"cannot list {proc_root}", file=sys.stderr)
        return 2
    for pid, age, resume, is_self, start in found:
        print(f"{pid}\t{age}\t{resume}\t{'self' if is_self else '-'}\t{start}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
