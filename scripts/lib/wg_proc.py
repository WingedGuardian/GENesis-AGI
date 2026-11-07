"""Process identity for the watchgod, read through one handle per process.

Run as `python3 -I -S wg_proc.py <proc root> holders|chain`, stdlib only, like
the
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

chain: stdin has one process per line, "<pid> <starttime>" ("-" for the
start time: whatever process holds the pid now). For each, print

    pid TAB status TAB comm TAB state TAB threads TAB depth TAB root TAB ancestry

walking up its parents to the nearest process named `claude`. Every link is
bound the same way as a holder's parent: a parent counts only if the child
still has it after the parent's record was read. status is
  ok       a claude ancestor was found; the process is not claude itself;
  claude   the process IS claude;
  nocc     no claude ancestor (init, or 64 hops, reached first);
  gone     the process exited, or is not the one with that start time;
  unknown  something on the way could not be read or changed while read.
depth is the hops to that claude, root the "pid:starttime" of the ancestor
whose parent is that claude (the process itself at depth 1), and ancestry
every "pid:starttime" from the process up to root, comma-separated. state and
threads are the process's own (stat fields 3 and 20), and comm its name made
safe for a page. Fields that do not apply are "-".
"""

from __future__ import annotations

import errno
import os
import sys
import unicodedata


def _gone(exc: OSError) -> bool:
    """True when the error means the process (or the descriptor) is gone. Any
    other failure (EACCES, say) means it could not be inspected: never "gone"
    (review finding)."""
    return exc.errno in (errno.ENOENT, errno.ESRCH)


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
        if exc.errno == errno.ESRCH:  # through the handle: only ESRCH means exited
            return None
    return ("?", "?", comm, ppid)


def _stat_fields(dfd: int) -> tuple[str, str, str, str] | None:
    """(starttime, state, comm, ppid) through the handle; None when the process
    has exited; "?" for what cannot be read or parsed."""
    try:
        raw = _read("stat", dfd)
    except OSError as exc:
        if exc.errno == errno.ESRCH:  # exited; anything else falls back
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


def _threads(dfd: int) -> str:
    try:
        tail = _read("stat", dfd).rpartition(b")")[2].split()
    except OSError:
        return "?"
    return tail[17].decode() if len(tail) > 17 and tail[17].isdigit() else "?"


def _chain_one(root: str, pid: str, want: str) -> str:
    try:
        dfd = os.open(f"{root}/{pid}", os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        if _gone(exc):
            return f"{pid}\tgone\t-\t-\t-\t-\t-\t-"
        return f"{pid}\tunknown\t-\t-\t-\t-\t-\t-"
    try:
        ident = _stat_fields(dfd)
        if ident is None or (want != "-" and ident[0] != want):
            return f"{pid}\tgone\t-\t-\t-\t-\t-\t-"
        start, state, comm, ppid = ident
        head = f"{pid}\t%s\t{comm}\t{state}\t{_threads(dfd)}"
        if start == "?":
            return head % "unknown" + "\t-\t-\t-"
        if comm == "claude":
            return head % "claude" + "\t-\t-\t-"
        links = [f"{pid}:{start}"]
        own = True  # dfd is still the process asked about
        for depth in range(1, 65):
            if not ppid.isdigit() or int(ppid) <= 1:
                return head % "nocc" + "\t-\t-\t-"
            try:
                pdfd = os.open(f"{root}/{ppid}", os.O_RDONLY | os.O_DIRECTORY)
            except OSError:
                return head % "unknown" + "\t-\t-\t-"
            parent = _stat_fields(pdfd)
            again = _stat_fields(dfd)
            if again is None and own:
                os.close(pdfd)
                return f"{pid}\tgone\t-\t-\t-\t-\t-\t-"
            if parent is None or again is None or again[3] != ppid or parent[0] == "?":
                os.close(pdfd)
                return head % "unknown" + "\t-\t-\t-"
            if parent[2] == "claude":
                os.close(pdfd)
                return head % "ok" + f"\t{depth}\t{links[-1]}\t{','.join(links)}"
            os.close(dfd)
            dfd = pdfd
            own = False
            links.append(f"{ppid}:{parent[0]}")
            ppid = parent[3]
        return head % "nocc" + "\t-\t-\t-"
    finally:
        os.close(dfd)


def chain(root: str, lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not (parts[1].isdigit() or parts[1] == "-"):
            continue
        out.append(_chain_one(root, parts[0], parts[1]))
    return out


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
        # A holder that cannot be inspected (EACCES, say) is still a holder
        # the walk found: it is printed with an unknown identity ("?"), so the
        # file stays watched and nothing is acted on without its start time
        # (review finding). Only a disappearance drops it.
        unknown = "\t".join(("?",) * 5)
        try:
            dfd = os.open(f"{root}/{pid}", os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            if not _gone(exc):
                for key in dict.fromkeys(k for k, _ in cands):
                    out.append(f"{key}\t{pid}\t{unknown}")
            continue
        try:
            held = []
            unread = []
            for key, fd in cands:
                try:
                    st = os.stat(f"fd/{fd}", dir_fd=dfd)
                    if f"{st.st_dev}:{st.st_ino}" == key and _writer(dfd, fd) and key not in held:
                        held.append(key)
                except OSError as exc:
                    if not _gone(exc) and key not in unread:
                        unread.append(key)
                    continue  # descriptor closed, or the process exited
            if held:
                ident = _stat_fields(dfd)
                if ident is None:
                    continue  # exited: nothing it held is held
                start, state, comm, ppid = ident
                pid_p, pcomm = _parent(root, dfd, ppid)
                for key in held:
                    out.append("\t".join((key, pid, start, state, comm, pid_p, pcomm)))
            # The unknown rows come LAST: the watchgod keeps one identity per
            # pid, the last row it reads, so a pid with any file it could not
            # read stays unknown and is never acted on (review finding).
            for key in unread:
                if key not in held:
                    out.append(f"{key}\t{pid}\t{unknown}")
        finally:
            os.close(dfd)
    return out


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[2] == "safe":
        sys.stdout.write(_safe(sys.stdin.buffer.read()) + "\n")
        return 0
    verbs = {"holders": holders, "chain": chain}
    if len(argv) != 3 or argv[2] not in verbs:
        print("usage: wg_proc.py <proc root> holders|chain|safe", file=sys.stderr)
        return 2
    lines = sys.stdin.read().splitlines()
    for row in verbs[argv[2]](argv[1], lines):
        sys.stdout.write(row + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
