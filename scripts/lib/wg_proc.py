"""Process identity for the watchgod, read through one handle per process.

Run as `python3 -I -S wg_proc.py <proc root> holders`, stdlib only, like the
rest of the watchgod's python: it must work when the venv does not.
`python3 -I -S wg_proc.py - safe` prints its stdin made safe for a page (see
below), for text that is not a process name: a file path, a session id.

Why a handle: a pid alone names whichever process holds the number when it is
read. Reading /proc/<pid>/fd/N and then /proc/<pid>/stat by path can see two
different processes if the first exits and its pid is reused in between. Here
/proc/<pid> is opened ONCE as a directory and every read goes through that
descriptor; once that process has exited, reads through it fail (ESRCH) instead
of reaching the pid's next owner. So the start time printed is the start time
of the process that held the file.

holders: stdin has one candidate per line, "<dev:ino> <pid> <fd>" (dev and
ino in decimal, as find %D:%i and stat %d:%i print them). For each candidate
whose descriptor still leads to that file and is open for writing, print

    <dev:ino> TAB pid TAB starttime TAB state TAB comm TAB ppid TAB pcomm

comm and pcomm are made safe for a page: every byte that is not UTF-8, and
every character Unicode files as a control, format, surrogate, private-use or
unassigned character (category C*: C0, DEL, C1, bidi overrides, zero-width
characters) or as a line or paragraph separator (U+2028, U+2029), becomes "?". A field that cannot be read while
the process is still there is "?" (cannot tell): never a guess. A candidate
that no longer holds the file, or whose process has exited, is left out.
All candidates of one pid are read through ONE handle, so every row printed
for a pid describes the same process. The parent is named only if the holder
still has the same parent after the parent was read; otherwise both parent fields are "?". A parent that cannot
be opened keeps the holder's own ppid, with its name "?".
"""

from __future__ import annotations

import errno
import os
import sys
import unicodedata


def _safe(raw: bytes) -> str:
    # A renderer may break a line at, or reorder text around, any of these: a
    # process-chosen name must never be able to start a line of its own or
    # disguise what follows it (review findings).
    text = raw.decode("utf-8", errors="replace")
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("Zl", "Zp") or ch == "\ufffd":
            out.append("?")
        else:
            out.append(ch)
    return "".join(out) or "?"


def _read(name: str, dfd: int) -> bytes:
    fd = os.open(name, os.O_RDONLY, dir_fd=dfd)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _fallback(dfd: int) -> tuple[str, str, str, str] | None:
    """No usable stat record: name and parent from comm and status, still
    through the handle; start time and state unknown."""
    comm, ppid = "?", "?"
    try:
        comm = _safe(_read("comm", dfd).rstrip(b"\n"))
        for line in _read("status", dfd).splitlines():
            if line.startswith(b"PPid:"):
                value = line.split(b":", 1)[1].strip()
                ppid = value.decode() if value.isdigit() else "?"
                break
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
    return ("?", "?", comm, ppid)


def _stat_fields(dfd: int) -> tuple[str, str, str, str] | None:
    """(starttime, state, comm, ppid) through the handle; None when the process
    has exited; "?" for what cannot be read or parsed."""
    try:
        raw = _read("stat", dfd)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
        return _fallback(dfd)
    head, sep, tail = raw.rpartition(b")")
    fields = tail.split()
    if not sep or b"(" not in head or len(fields) < 20:
        return _fallback(dfd)
    comm = _safe(head.split(b"(", 1)[1])
    st, state, ppid = fields[19], fields[0], fields[1]
    if not (st.isdigit() and ppid.isdigit()):
        return ("?", "?", comm, "?")
    return (st.decode(), _safe(state), comm, ppid.decode())


def _writer(dfd: int, fd: str) -> bool:
    for line in _read(f"fdinfo/{fd}", dfd).splitlines():
        if line.startswith(b"flags:"):
            flags = line.split(b":", 1)[1].strip()
            return bool(flags) and all(48 <= c <= 55 for c in flags) and int(flags, 8) & 3 != 0
    return False


def _parent(root: str, dfd: int, ppid: str) -> tuple[str, str]:
    """(ppid, pcomm), bound: the holder must still have this parent after the
    parent's record was read, or the parent may be a reused pid."""
    if not ppid.isdigit() or int(ppid) <= 0:
        return ("?", "?")
    try:
        pdfd = os.open(f"{root}/{ppid}", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        # The holder's own record names this parent; only its name is unknown.
        return (ppid, "?")
    try:
        parent = _stat_fields(pdfd)
    finally:
        os.close(pdfd)
    again = _stat_fields(dfd)
    if parent is None or again is None or again[3] != ppid:
        return ("?", "?")
    return (ppid, parent[2])


def holders(root: str, lines: list[str]) -> list[str]:
    # Grouped by pid, in first-seen order: one handle per pid, so a pid reused
    # between two of its candidates can never lend its identity to the first
    # one's file (review finding).
    by_pid: dict[str, list[tuple[str, str]]] = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
            continue
        key, pid, fd = parts
        by_pid.setdefault(pid, []).append((key, fd))
    out = []
    for pid, cands in by_pid.items():
        try:
            dfd = os.open(f"{root}/{pid}", os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            continue  # gone, or never there
        try:
            held = []
            for key, fd in cands:
                try:
                    st = os.stat(f"fd/{fd}", dir_fd=dfd)
                    if f"{st.st_dev}:{st.st_ino}" == key and _writer(dfd, fd) and key not in held:
                        held.append(key)
                except OSError:
                    continue  # descriptor closed, or the process exited
            if not held:
                continue
            ident = _stat_fields(dfd)
            if ident is None:
                continue
            start, state, comm, ppid = ident
            pid_p, pcomm = _parent(root, dfd, ppid)
            for key in held:
                out.append("\t".join((key, pid, start, state, comm, pid_p, pcomm)))
        finally:
            os.close(dfd)
    return out


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[2] == "safe":
        sys.stdout.write(_safe(sys.stdin.buffer.read()) + "\n")
        return 0
    if len(argv) != 3 or argv[2] != "holders":
        print("usage: wg_proc.py <proc root> holders|safe", file=sys.stderr)
        return 2
    lines = sys.stdin.read().splitlines()
    for row in holders(argv[1], lines):
        sys.stdout.write(row + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
