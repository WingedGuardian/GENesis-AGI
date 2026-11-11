#!/usr/bin/env python3
"""Heal a `live` checkout a killed switch left half-written (#3492 PR D).

Recovery must not wait for a person and must never start code bootstrap did
not run on, so the repair only goes back toward the rollback tag, and loses
nothing: every node it changes is copied to deploy-backups/<ts>-live-move-repair/
first. A node the update did not write is restored too (leaving it would leave
the tree mixed) and reported, with the restore command that puts it back
(never a shell step: #3868). The path set,
the two-dot diff a switch writes, contains everything the guard counts as
residue, and only the guard's SAFE afterwards clears the state. With HEAD on
`live` at the target (a rollback killed partway, PR D1f) the repair finishes
that rollback: the files, then `live` moved back to the tag by compare-and-swap,
then the git hooks restored (retried by every later run while HEAD is the tag
on `live`). A target whose objects a power loss took cannot be judged, so the
whole tree is restored to the tag and a clean tree clears it. Run from a kit
outside the checkout (the checkout is what may be half-written): python3 -I -S,
standard library only.
Exit 1 only where a start would run a half-written tree; at boot every failure
before the checkout itself changes exits 0 (the server starts as it did before),
a failed backup included. A main row is out of scope (QD3): exit 3 by command, 0
at boot."""

from __future__ import annotations

import argparse
import calendar
import contextlib
import errno
import fcntl
import functools
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import types

_LIB = os.path.dirname(os.path.abspath(__file__))
BOOT_BUDGET_S = 60  # every wait at boot, inside systemd's default 90 s start timeout
_POLL_S = 1.0
LOCK_FRESH_S = 30  # see _held_open; under BOOT_BUDGET_S, so a boot just after a kill still heals
# _held_open re-checks a holder it found only through an unreadable fd table
# this many times, this far apart (about 0.45 s, and only when one exists).
_CONFIRM_SCANS, _CONFIRM_STEP_S = 3, 0.15
_PROC = "/proc"
SUFFIX = "-live-move-repair"
_now, _sleep = time.monotonic, time.sleep  # a test's fake clock replaces both
# _confirmed's wait is real time even under a fake clock: the processes it
# re-checks run on the real one.
_settle = time.sleep
_changed = False  # set when the checkout itself starts to change (_apply)


class TooLarge(Exception):
    """A node over a reader's byte or entry budget (the restore check's)."""


class RepairError(Exception):
    pass


def _log(msg: str) -> None:
    print(f"live-move repair: {msg}", file=sys.stderr, flush=True)


#: Every git call's options: no fsmonitor program and no hook from the repository's config.
GIT_C = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


def _git(repo: str, *args: str, check: bool = True, data: bytes | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", *GIT_C, "-C", repo, *args], capture_output=True, input=data, env=env)
    if check and p.returncode != 0:
        why = p.stderr.decode(errors="replace").strip()
        raise RepairError(f"git {args[0]} failed (exit {p.returncode}): {why}")
    return p


def _out(repo: str, *args: str, check: bool = True) -> str:
    return os.fsdecode(_git(repo, *args, check=check).stdout.strip())


def _z(out: bytes) -> list[str]:
    return [os.fsdecode(x) for x in out.split(b"\0") if x]


def _tree(repo: str, rev: str) -> dict[str, tuple[str, str]]:
    """path -> (mode, oid) of every blob and gitlink in ``rev``."""
    entries = {}
    for rec in _z(_git(repo, "ls-tree", "-r", "-z", "--full-tree", rev).stdout):
        meta, path = rec.split("\t", 1)
        mode, _typ, oid = meta.split()
        entries[path] = (mode, oid)
    return entries


def _parents(p: str) -> list[str]:
    out, d = [], os.path.dirname(p)
    while d:
        out.append(d)
        d = os.path.dirname(d)
    return out


def _link_parent(repo: str, p: str) -> str | None:
    """The parent of ``p`` that is a symlink or a non-directory, if any. Then
    ``p`` is not in the checkout: an lstat or unlink of it would act THROUGH
    that parent (a symlink can point outside the checkout), and git itself
    calls such a path absent."""
    for d in _parents(p):
        full = os.path.join(repo, d)
        if os.path.islink(full) or (os.path.lexists(full) and not os.path.isdir(full)):
            return d
    return None


def _beyond_link(repo: str, p: str) -> bool:
    return _link_parent(repo, p) is not None


def _blob_id(full: str, st: os.stat_result, fmt: str, budget=None) -> str:
    """The blob id of the regular file ``st`` (its lstat) is, read in 1 MiB
    chunks from an fd opened without following a link or blocking on a FIFO,
    and only while it is still that file: a node swapped after the lstat (a
    FIFO, a link, another file) or changed while read raises OSError, never
    blocks or reads something else. ``budget(size)`` may refuse it first."""
    fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        now = os.fstat(fd)
        if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            raise OSError(errno.ESTALE, "replaced after it was looked at", full)
        if budget is not None:
            budget(now.st_size)
        h, left = hashlib.new(fmt, b"blob %d\0" % now.st_size), now.st_size
        while left > 0 and (chunk := os.read(fd, min(1 << 20, left))):
            h.update(chunk)
            left -= len(chunk)
        if left or os.read(fd, 1):
            raise OSError(errno.ESTALE, "changed while it was read", full)
        return h.hexdigest()
    finally:
        os.close(fd)


def _node(repo: str, p: str, fmt: str, budget=None) -> tuple[str, str] | None:
    """(mode, blob id) of the disk node, ("dir", "") or ("other", ""); None if absent,
    or beyond a symlinked or non-directory parent (never read through one).
    A regular file is read as _blob_id reads it, under ``budget`` if given."""
    full = os.path.join(repo, p)
    if _beyond_link(repo, p):
        return None
    try:
        st = os.lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(st.st_mode):
        mode, data = "120000", os.fsencode(os.readlink(full))
    elif stat.S_ISREG(st.st_mode):
        # The executable bit is the one permission git tracks (a 0600 file reads
        # unchanged to `git status`, MEASURED git 2.43): a restore gives other bits
        # the umask's, as any checkout does, and only to the tag's own bytes.
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        return mode, _blob_id(full, st, fmt, budget)
    else:
        return ("dir" if stat.S_ISDIR(st.st_mode) else "other", "")
    return mode, hashlib.new(fmt, b"blob %d\0" % len(data) + data).hexdigest()


def _as_disk(entry):
    return ("dir", "") if entry and entry[0] == "160000" else entry  # a gitlink is a directory


def _as_index(entry) -> list[tuple[str, str, str]]:
    return [(entry[0], entry[1], "0")] if entry else []


_KINDS = (
    (stat.S_ISFIFO, "fifo"),
    (stat.S_ISSOCK, "socket"),
    (stat.S_ISCHR, "char-device"),
    (stat.S_ISBLK, "block-device"),
)


def _special(full: str) -> dict:
    """The type and permission bits of a node that holds no bytes to copy."""
    st = os.lstat(full)
    kind = next((name for test, name in _KINDS if test(st.st_mode)), "unknown")
    return {"type": kind, "mode": f"{stat.S_IMODE(st.st_mode):04o}"}


#: A saved file over the restore check's per-file budget (live_move_restore's
#: FILE_BYTES; a test holds them equal) is recorded with no hash: the restore
#: refuses it (`too-large`) rather than read it.
HASH_BYTES = 256 * 1024 * 1024
#: SAVED.json's cap, as REPORT_BYTES is REPAIR.json's. A file's record is
#: about 110 bytes plus its path (MEASURED: 207 per record with 96-byte paths),
#: so this holds the restore check's ENTRIES (200,000) records of paths up to
#: about 55 bytes; past it a copy goes unrecorded, which the restore refuses.
SAVED_BYTES = 32 * 1024 * 1024
#: The alert body's cap. queue_alert gets it as one argv string and passes it
#: on as one environment string, and one of 131,072 bytes or more is E2BIG
#: (MEASURED, Linux MAX_ARG_STRLEN; `getconf ARG_MAX`, 2,097,152, bounds all of
#: them together): 16 KiB each leaves room for any environment. The full text
#: is in REPAIR.json.
ALERT_BYTES = 16 * 1024


class _Saved:
    """SAVED.json, the integrity record of a report's saved copies: per path
    under the backup dir, a regular file's sha256 (null over HASH_BYTES) and
    the mode the checkout had, a symlink's target, a FIFO's mode. Past
    SAVED_BYTES nothing more is recorded and `complete` says so: a copy with
    no record has no hash, which a restore refuses."""

    def __init__(self, backup: str) -> None:
        self.backup, self.files, self.complete = backup, {}, True
        self.left = SAVED_BYTES - len('{"files": {}, "complete": false}')

    def add(self, dst: str, record: dict) -> None:
        key = os.path.relpath(dst, self.backup)
        cost = len(json.dumps(key)) + len(json.dumps(record)) + 4  # ": " and ", "
        if cost > self.left:
            self.complete = False
            return
        self.left -= cost
        self.files[key] = record

    def data(self) -> dict:
        return {"files": self.files, "complete": self.complete}


#: What shutil.copy2 skips when copying extended attributes (shutil._copyxattr,
#: Python 3.12): listing on a file system that has none, and setting one this
#: user may not set or the target file system does not take.
_XATTR_LIST_SKIP = frozenset({errno.ENOTSUP, errno.ENODATA, errno.EINVAL})
_XATTR_SET_SKIP = _XATTR_LIST_SKIP | {errno.EPERM, errno.EACCES}


def _copy_xattrs(src_fd: int, dst_fd: int) -> None:
    """The extended attributes copy2 copied, from fd to fd. The repair deletes
    the original, so what the copy lacks is gone; one this user may not set
    (security.*, trusted.*) is skipped, as copy2 skips it."""
    if not hasattr(os, "listxattr"):
        return
    try:
        names = os.listxattr(src_fd)
    except OSError as exc:
        if exc.errno in _XATTR_LIST_SKIP:
            return
        raise
    for name in names:
        try:
            os.setxattr(dst_fd, name, os.getxattr(src_fd, name))
        except OSError as exc:
            if exc.errno not in _XATTR_SET_SKIP:
                raise


def _copy_file(src: str, dst: str) -> dict:
    """Copy the regular file ``src`` to a new ``dst``, hashing the bytes as they
    are written (one read: a boot repair runs inside BOOT_BUDGET_S), from an fd
    opened without following a link or blocking on a FIFO; times kept as copy2
    keeps them, the mode without group or world write. Returns its record."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.ESTALE, "no longer a regular file", src)
        h, size = hashlib.sha256(), 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        out = os.open(dst, flags, 0o600)
        try:
            while chunk := os.read(fd, 1 << 20):
                size += len(chunk)
                h.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view) :]
            _copy_xattrs(fd, out)
        finally:
            os.close(out)
    finally:
        os.close(fd)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    mode = stat.S_IMODE(st.st_mode)
    os.chmod(dst, mode & ~0o022)
    sha = h.hexdigest() if size <= HASH_BYTES else None
    return {"type": "file", "mode": f"{mode:04o}", "sha256": sha}


def _copy_node(src: str, dst: str, unsaved: list[dict], root: str, saved: _Saved) -> None:
    """copytree's copy function, and every saved node's: a file is copied and
    hashed, a symlink and a FIFO (it holds no bytes) made again, each recorded
    in ``saved``; a socket or a device cannot be, so it is recorded in
    ``unsaved`` instead of failing the whole copy (copy2 raises on both)."""
    st = os.lstat(src)
    if stat.S_ISREG(st.st_mode):
        saved.add(dst, _copy_file(src, dst))
    elif stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), dst)
        saved.add(dst, {"type": "symlink", "target": os.readlink(dst)})
    elif stat.S_ISFIFO(st.st_mode):
        mode = stat.S_IMODE(st.st_mode)
        os.mkfifo(dst)
        os.chmod(dst, mode & ~0o022)
        saved.add(dst, {"type": "fifo", "mode": f"{mode:04o}"})
    else:
        unsaved.append({"path": os.path.relpath(src, root), **_special(src)})


def _index_state(repo: str) -> tuple[dict, dict]:
    """path -> [(mode, oid, stage)] of the index, and path -> its `ls-files -v`
    tag when flagged: S skip-worktree, lowercase assume-unchanged, s both."""
    index: dict[str, list[tuple[str, str, str]]] = {}
    for rec in _z(_git(repo, "ls-files", "-s", "-z").stdout):
        meta, path = rec.split("\t", 1)
        mode, oid, stage = meta.split()
        index.setdefault(path, []).append((mode, oid, stage))
    listing = _z(_git(repo, "ls-files", "-v", "-z").stdout)
    flags = {r[2:]: r[0] for r in listing if r[0] == "S" or r[0].islower()}
    return index, flags


def _pid_alive(pid: int) -> bool:
    """Running and not a zombie (deploy_marker.sh's _deploy_marker_holder_live)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"{_PROC}/{pid}/stat") as fh:
            return fh.read().rsplit(") ", 1)[-1][:1] != "Z"
    except OSError:
        return True


def _exiting(pid: int) -> bool:
    """Is PF_EXITING (0x4, field 9 of /proc/<pid>/stat) set? The process is in
    the kernel's exit path and runs no user code again, so it can neither commit
    nor remove a lock. The kernel sets it before releasing the memory map, which
    is what makes an exiting process's fds unreadable (MEASURED 2026-11-07, six
    `git status` loops for 20 s: 484 of 484 unreadable sightings had it). It is the main thread's flag;
    git never ends its main thread first. False when the stat cannot be read."""
    try:
        with open(f"{_PROC}/{pid}/stat") as fh:
            return bool(int(fh.read().rsplit(")", 1)[1].split()[6]) & 0x4)
    except (OSError, IndexError, ValueError):
        return False


def _may_hold(pid: int) -> bool:
    """Could ``pid`` still hold a lock: alive, not a zombie, not exiting."""
    return _pid_alive(pid) and not _exiting(pid)


def _flock(path: str, deadline: float) -> int | None:
    """flock, as every other holder of update.lock takes it; None if still held at ``deadline``.
    A lock path that is a symlink (never followed) or not a regular file is refused."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        raise RepairError(f"{path} is a symlink; it is not taken as a lock") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RepairError(f"{path} is not a regular file; it is not taken as a lock")
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if _now() >= deadline:
                os.close(fd)
                return None
            _sleep(min(_POLL_S, max(deadline - _now(), 0)))


def _held_open(path: str) -> bool:
    """Does a process hold ``path`` open (git holds index.lock open from creation)?
    Readable fd tables are matched exactly. Other users' fds are never readable
    here (MEASURED: /proc/1/fd is EACCES), nor is a zombie's or an exiting
    process's (neither holds anything: _may_hold).
    A live process of this user whose fds cannot be read (non-dumpable: (sd-pam),
    gpg-agent, tailscaled, MEASURED on a systemd-user install) counts only if it
    is a git (its comm stays readable) or the lock is younger than LOCK_FRESH_S
    (a tool may write the index through a library; a real writer holds it for
    ms to seconds). Residual: a non-dumpable non-git process holding a lock
    longer than that has it judged stale. A ref lock's fd is closed once its
    value is written, so a ref transaction in flight is seen only through the
    same freshness bound; update.lock and genesis-checkout.lock, held here,
    keep Genesis's own movers out. An unreadable process counts only once
    _confirmed has seen it last."""
    try:
        fresh = time.time() - os.lstat(path).st_mtime < LOCK_FRESH_S
    except OSError:
        fresh = False
    suspects: dict[int, str | None] = {}  # unreadable: confirmed by _confirmed
    for pid in os.listdir(_PROC):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            if os.stat(f"{_PROC}/{pid}").st_uid != os.getuid():
                continue
            fds = os.listdir(f"{_PROC}/{pid}/fd")
        except PermissionError:
            comm = ""
            with contextlib.suppress(OSError), open(f"{_PROC}/{pid}/comm") as fh:
                comm = fh.read()
            if (fresh or comm.startswith("git")) and _may_hold(int(pid)):
                suspects[int(pid)] = _start_time(int(pid))
            continue
        except OSError:
            continue
        if _fd_names(pid, path, fds):
            return True
    return _confirmed(suspects, path)


def _fd_names(pid, path: str, fds: list[str] | None = None) -> bool:
    """Does process ``pid`` have ``path`` open? False when its fds cannot be read."""
    if fds is None:
        try:
            fds = os.listdir(f"{_PROC}/{pid}/fd")
        except OSError:
            return False
    for fd in fds:
        with contextlib.suppress(OSError):
            if os.readlink(f"{_PROC}/{pid}/fd/{fd}") == path:
                return True
    return False


def _start_time(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (start time in clock ticks), or None."""
    try:
        with open(f"{_PROC}/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _confirmed(suspects: dict[int, str | None], path: str) -> bool:
    """Is any unreadable suspect a lasting holder rather than a passing one?

    A process whose fd table is unreadable only for a moment, counted from one
    scan, made a stale lock look held, and the heal left it (#4001; on a box
    running many git sessions, and between pytest-xdist workers). An exiting
    process, the measured case, is dropped at the scan (_may_hold); this re-check
    covers any other passing one. A suspect still counts only if it is the same
    process (same start time), still able to hold, and still unreadable after each of
    _CONFIRM_SCANS re-checks _CONFIRM_STEP_S apart; if its fds become readable
    it is judged on them. A real holder stays, so this stays on the safe side.
    """
    for _ in range(_CONFIRM_SCANS):
        if not suspects:
            return False
        _settle(_CONFIRM_STEP_S)
        for pid, start in list(suspects.items()):
            if not _may_hold(pid) or _start_time(pid) != start:
                del suspects[pid]
                continue
            try:
                os.listdir(f"{_PROC}/{pid}/fd")
            except PermissionError:
                continue  # still unreadable: still a suspect
            except OSError:
                del suspects[pid]
                continue
            if _fd_names(pid, path):
                return True
            del suspects[pid]
    return bool(suspects)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: str, data: dict, private: bool = False, indent: int | None = 2) -> None:
    """Atomically and durably; ``private``: mode 0600 (a report's files), the
    temp file's mode from its creation."""
    tmp = f"{path}.{os.getpid()}.tmp"
    # The temp name is guessable: a node left or planted there is removed (a
    # link, not its target) and the temp made fresh, never followed.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        with open(os.open(tmp, flags, 0o600 if private else 0o666), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=indent)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(os.path.dirname(path))


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


#: A report dir's name is its ts (_backup_dir: the UTC time, then the pid)
#: followed by SUFFIX. ASCII digits only: \d also matches other scripts' digits.
_TS_FORMAT = "%Y%m%dT%H%M%SZ"
REPORT_NAME = re.compile(r"([0-9]{8}T[0-9]{6}Z-[0-9]+)" + re.escape(SUFFIX))
#: A report over either cap is not read. The writer stored about 615 bytes per
#: engine row and 954 per foreign row while it kept a reapply step per foreign
#: row (indent 2, MEASURED); without it (#3868) a whole report measured 740
#: bytes per row with 96-byte paths (231,778 for 313 rows), so REPORT_BYTES
#: still binds first, at roughly 5,000-6,800 rows; REPORT_ROWS is a backstop
#: for a report the writer did not make.
REPORT_BYTES = 4 * 1024 * 1024
REPORT_ROWS = 20_000


class BadReport(Exception):
    """A report dir or REPAIR.json the writer does not make; never read further."""


class NotThisCheckout(BadReport):
    """A report whose ``repo`` names another checkout (or none)."""


def report_ts(name: str) -> str | None:
    """The ts of a report dir named as _backup_dir names it, else None."""
    m = REPORT_NAME.fullmatch(name)
    return m.group(1) if m else None


#: A report dir named more than this many seconds ahead of the reader's clock
#: is not a report (written while the clock ran ahead, as at a boot before NTP
#: sync): no reader takes it as the newest. Deploy health refuses it; the
#: guardian's pick skips it (collector._REPAIR_FUTURE_S, held equal by a test).
REPORT_FUTURE_S = 300


def report_order(ts: str) -> tuple[int, int]:
    """A report ts (``YYYYmmddTHHMMSSZ-<pid>``) as the readers' one sort key:
    its UTC seconds, then its pid as a number (so pid 10000 is newer than 9999
    in the same second). ValueError if the ts names no date or pid."""
    stamp, pid = ts.split("-", 1)
    return calendar.timegm(time.strptime(stamp, _TS_FORMAT)), int(pid)


def load_report(path: str, repo: str) -> dict | None:
    """One report, read as untrusted input: the readers' single way in.

    ``path`` is the report dir, ``repo`` the deploy checkout the reader serves.
    The dir must be a directory (never a symlink) named as _backup_dir names
    it; REPAIR.json is opened without following a symlink or blocking on a
    FIFO, must be a regular file of at most REPORT_BYTES holding a JSON
    object; its ``repo`` must name ``repo`` (see same_repo: no reader runs git
    in, or reports for, a checkout a report names); ``paths`` a list of at
    most REPORT_ROWS entries. Returns {"ts", "report", "rows" (the dict rows),
    "mtime"}; None when the dir holds no REPAIR.json yet (a run that wrote
    none, or is writing it); raises BadReport for anything else."""
    ts = report_ts(os.path.basename(path))
    if ts is None:
        raise BadReport("not a name the repair writes")
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise BadReport("not a directory")
        fd = os.open(os.path.join(path, "REPAIR.json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BadReport(f"cannot be opened ({exc.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BadReport("REPAIR.json is not a regular file")
        chunks, size = [], 0
        while size <= REPORT_BYTES and (chunk := os.read(fd, REPORT_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        raise BadReport(f"cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)
    if size > REPORT_BYTES:
        raise BadReport(f"over {REPORT_BYTES} bytes")
    try:
        report = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        raise BadReport("not JSON") from None
    if not isinstance(report, dict):
        raise BadReport("not a JSON object")
    if not same_repo(report.get("repo"), repo):
        raise NotThisCheckout("not this checkout's report")
    paths = report.get("paths")
    if not isinstance(paths, list) or len(paths) > REPORT_ROWS:
        raise BadReport(f"paths is not a list of at most {REPORT_ROWS} rows")
    rows = [r for r in paths if isinstance(r, dict)]
    return {"ts": ts, "report": report, "rows": rows, "mtime": st.st_mtime}


#: Longest ``repo`` a report may name (PATH_MAX); a longer one is refused
#: before realpath walks it.
REPO_BYTES = 4096


#: Written beside a REPAIR.json no reader can read (load_report refuses it) by
#: --dismiss TS, which then cannot record dismissed_at inside it. Every reader
#: (deploy health, the guardian's pick, the deploy-backups prune) skips a report
#: folder holding it as a regular file (#4331 review): the report's alert
#: clears and the prune takes the folder 90 days after its last write.
DISMISS_MARK = "DISMISSED.json"


def dismiss_marked(path: str) -> bool:
    """Does the report dir ``path`` hold DISMISS_MARK as a regular file?"""
    try:
        return stat.S_ISREG(os.lstat(os.path.join(path, DISMISS_MARK)).st_mode)
    except OSError:
        return False


def same_repo(named, repo: str) -> bool:
    """Whether a report's ``repo`` field names ``repo``. The field is untrusted:
    it must be a str with no NUL whose file-system bytes (os.fsencode: a
    non-UTF-8 path the writer stored round-trips as surrogate escapes; any
    other lone surrogate cannot encode and is refused) number at most
    REPO_BYTES, checked before realpath runs; a realpath error is a refusal."""
    if not isinstance(named, str) or "\0" in named:
        return False
    try:
        if len(os.fsencode(named)) > REPO_BYTES:
            return False
        return os.path.realpath(named) == os.path.realpath(repo)
    except (UnicodeError, ValueError, OSError):
        return False


def manifests(backups: str) -> list[tuple[str, dict]]:
    """(path, REPAIR.json) of every repair, oldest first."""
    try:
        names = sorted(n for n in os.listdir(backups) if n.endswith(SUFFIX))
    except OSError:
        return []
    found = [os.path.join(backups, n, "REPAIR.json") for n in names]
    return [(p, m) for p, m in ((p, _load(p)) for p in found) if m is not None]


def _ephemeral() -> str:
    """EPHEMERAL_DIRTY_RE; unreadable, a pattern matching nothing (bytes then decide)."""
    p = subprocess.run(
        ["bash", "-c", '. "$1/deploy_marker.sh" && printf %s "$EPHEMERAL_DIRTY_RE"', "re", _LIB],
        capture_output=True,
        text=True,
    )
    return p.stdout if p.returncode == 0 and p.stdout else r"(?!)"


def _load_guard():
    return _load_kit("update_state_guard.py")


def _load_kit(name: str):
    """A kit source beside this one (the guard; live_move_restore.py, which
    loads live_move_restore_rows.py and live_move_restore_runs.py beside it,
    and live_move_restore_write.py)."""
    path = os.path.join(_LIB, name)
    spec = importlib.util.spec_from_file_location(f"_live_repair_{name[:-3]}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _update_lock(home: str) -> str:
    locks = os.path.join(os.environ.get("GENESIS_HOME") or home, "locks")
    os.makedirs(locks, exist_ok=True)
    return os.path.join(locks, "update.lock")


class Repair:
    def __init__(self, state_path: str, repo: str, boot: bool, wait: int):
        self.state_path, self.repo, self.boot, self.wait = state_path, repo, boot, wait
        self.home = os.path.dirname(os.path.abspath(state_path))
        self.backups = os.path.join(self.home, "deploy-backups")
        self.backups_warning: str | None = None
        self.guard = _load_guard()
        self._fds: list[int] = []
        self.update_fd: int | None = None  # update.lock's, handed to restore-hooks
        self.forward = False  # HEAD on `live` at the target: finishing a rollback

    def _hold(self, path: str, deadline: float) -> int | None:
        fd = _flock(path, deadline)
        if fd is not None:
            self._fds.append(fd)
        return fd

    def _say(self, msg: str) -> None:
        if self.boot:
            _log(msg)
        else:
            print(msg, flush=True)

    def run(self, restore: str | None = None, write: bool = False) -> int:
        try:
            return self._restore(restore, write) if restore is not None else self._run()
        finally:
            for fd in self._fds:
                os.close(fd)

    def _update_lock(self) -> str:
        return _update_lock(self.home)

    def _run(self) -> int:
        pending = self._pending_hooks()
        if not os.path.lexists(self.state_path) and not pending:
            self._say("nothing to repair: there is no update state")
            return 0
        deadline = _now() + (BOOT_BUDGET_S if self.boot else self.wait)
        lock = self._update_lock()
        self.update_fd = self._hold(lock, 0)
        if self.update_fd is None:
            if not os.path.lexists(self.state_path):
                return 0  # a deploy restarting its server under the lock; a later run retries
            pid = (self._state()[1] or {}).get("pid")
            if self.boot and isinstance(pid, int) and pid > 1 and _pid_alive(pid):
                # update.sh or a deploy starts its own server under the lock; a
                # failure here would only send it to its degraded fallback.
                _log(f"update.lock is held and its update (pid {pid}) runs; it starts the server")
                return 0
            self.update_fd = self._hold(lock, deadline)
            if self.update_fd is None:
                verdict, _ = self.guard.verdict(self.state_path, self.repo)
                self._say(f"update.lock is still held after the wait; the checkout is {verdict}")
                return 1 if verdict == self.guard.MIXED_TREE else 0
        # The checkout lock before the hooks retry too: restore-hooks runs under
        # it in the forward heal, as in update.sh's rollback.
        locks = self._checkout_locks(deadline)
        if locks is None:
            return 1 if os.path.lexists(self.state_path) else 0  # no state: a later run retries
        for path in pending:
            try:
                self._retry_hooks(path)
            except (OSError, RepairError) as exc:  # one report never stops the heal below
                _log(f"the git hooks retry for {path} was not recorded ({exc})")
        if not os.path.lexists(self.state_path):
            self._say("nothing to repair: there is no update state")
            return 0
        return self._judge_state(locks[0])

    def _checkout_locks(self, deadline: float) -> list[str] | None:
        """genesis-checkout.lock, then no process holding a git lock; the git
        locks' paths (index.lock first), or None (said why) at ``deadline``."""
        common = _out(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if self._hold(os.path.join(common, "genesis-checkout.lock"), deadline) is None:
            self._say("the checkout lock (genesis-checkout.lock) is still held after the wait")
            return None
        locks = [path for _name, path in self._locks()]  # index.lock first
        while held := [p for p in locks if os.path.lexists(p) and _held_open(p)]:
            if _now() >= deadline:
                self._say(
                    f"{held[0]} is still held, or too recent to call stale (under"
                    f" {LOCK_FRESH_S} s old), after the wait; run the repair again"
                )
                return None
            _sleep(_POLL_S)
        return locks

    def _restore(self, ts: str, write: bool) -> int:
        """--restore TS [--check], under the repair's own locks in its order (a
        short wait); never while the guard calls the checkout half-written or
        its update unfinished (MIXED_TREE, GUARDS: review LOW-1). --check
        judges what a restore would write and writes nothing; without it the
        restore writes (live_move_restore.restore)."""
        nothing = "nothing was written" if write else "nothing was judged"
        deadline = _now() + self.wait
        if self._hold(self._update_lock(), deadline) is None:
            pid = (_load(self.state_path) or {}).get("pid")
            who = f"; the update state names pid {pid}" if isinstance(pid, int) else ""
            self._say(f"update.lock is still held after the wait (an update runs{who}); {nothing}")
            return 1
        if self._checkout_locks(deadline) is None:
            return 1
        verdict, reason = self.guard.verdict(self.state_path, self.repo)
        if verdict in (self.guard.MIXED_TREE, self.guard.GUARDS):
            what = "half-written" if verdict == self.guard.MIXED_TREE else "mid-update"
            self._say(f"the checkout is {what} ({reason}); repair or finish it first, {nothing}")
            return 1
        kit = _load_kit("live_move_restore.py")  # the check and the judge
        rp = types.SimpleNamespace(**globals())
        if not write:
            return kit.check(rp, self.repo, self.home, ts, self._say)
        writes = _load_kit("live_move_restore_write.py")
        return writes.restore(rp, kit, self.repo, self.home, ts, self._say)

    def _state(self) -> tuple[bytes | None, dict | None]:
        """The state file's bytes and its object, through the guard's bounded
        reader (#3964): a link, a FIFO or an over-cap file is (None, None), an
        unreadable state, never a read that blocks while the locks are held."""
        try:
            raw = self.guard.read_state(self.state_path)
        except (OSError, ValueError):
            return None, None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return raw, None
        return raw, data if isinstance(data, dict) else None

    def _judge_state(self, index_lock: str) -> int:
        raw, state = self._state()
        verdict, reason = self.guard.state_verdict(state, self.repo)
        to = self._live_target(state)
        if verdict == self.guard.SAFE and to:
            # A repair killed after it healed, before it cleared. Its report keeps
            # no result; readers judge "unresolved" by the guard, not by it.
            # A switch killed before it wrote anything leaves only its locks.
            self._retire_locks(self._backup_dir())
            self._clear(raw)
            return 0
        if verdict != self.guard.MIXED_TREE:
            self._say(f"nothing to repair: the checkout is not half-written ({verdict}: {reason})")
            return 0
        if not to:
            self._say("a half-written checkout on main is not repaired here; only `live` rows are")
            return 0 if self.boot else 3
        # Judged by HEAD and the branch, as the guard's live rule judges them,
        # never by the guard's wording: off the tag, the guard calls a live row
        # half-written only with HEAD on `live` at the target (a rollback killed
        # partway), which the repair finishes (D1f).
        head = _out(self.repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
        if head != self.guard._tag_commit(state, self.repo):
            branch = _out(self.repo, "symbolic-ref", "-q", "HEAD", check=False)
            if head != to or branch != "refs/heads/live":
                self._say("the checkout is neither at its rollback tag nor on `live` at the target")
                return 1
            self.forward = True
        return self._heal(state, raw, to, index_lock)

    def _live_target(self, state: object) -> str | None:
        """The move's target of a live row (rule 6's record, or 6a's own_head)."""
        if not isinstance(state, dict) or state.get("phase") != "merging":
            return None
        move = self.guard._move_record(state)
        return move[0] if move else self.guard._adopted_tip(state, self.repo)

    def _heal(self, state: dict, raw: bytes, to: str, index_lock: str) -> int:
        repo = self.repo
        self.tag = tag = self.guard._tag_commit(state, repo)
        if _out(repo, "rev-parse", "--verify", "HEAD^{commit}") != (to if self.forward else tag):
            raise RepairError("HEAD moved while the repair judged it")
        self.fmt = _out(repo, "rev-parse", "--show-object-format")
        backup = self._backup_dir()
        ts = os.path.basename(backup)[: -len(SUFFIX)]
        torn = self._keep_index(backup)
        self.want, self.ephemeral = _tree(repo, tag), _ephemeral()
        index, flags = _index_state(repo)
        rows = None
        if _git(repo, "cat-file", "-e", f"{to}^{{tree}}", check=False).returncode == 0:
            # Classification reads the target's subtrees and blobs too; one a
            # power loss took below a readable root makes it unreadable.
            with contextlib.suppress(RepairError):
                rows = self._rows(to, index, flags)
        readable = rows is not None
        rows = rows if readable else self._rows(None, index, flags)
        manifest = {"ts": ts, "repo": repo, "state_row": state, "tag": tag, "other": to}
        # `reapply` stays [] (#3868: no generated shell) so older readers stay valid.
        manifest.update(result=None, paths=rows, reapply=[], dismissed_at=None, hooks=None)
        manifest.update(index_rebuilt=torn, integrity="SAVED.json")
        self._save(backup, manifest)
        self._apply(rows, index)
        if self.forward:
            # Recorded before the ref moves, so a kill after it leaves the hooks
            # to the next run's retry (which supersedes it if `live` never moved).
            manifest["hooks"] = "pending"
            # Every writer of a REPAIR.json holds update.lock (_run, _dismiss,
            # _restore): _retry_hooks writes back what it read under it.
            write_json(os.path.join(backup, "REPAIR.json"), manifest, private=True)
            msg = f"live-move repair: finish the rollback to {tag[:12]}"
            cas = _git(repo, "update-ref", "-m", msg, "refs/heads/live", tag, to, check=False)
            if cas.returncode != 0:
                manifest["hooks"] = None
                why = cas.stderr.decode(errors="replace").strip()
                result = f"unresolved: `live` could not be moved from {to[:12]} to the tag ({why})"
                return self._finish(backup, manifest, result, 1)
            manifest["hooks"] = self._put_hooks_back(backup, manifest, to, tag)
        _git(repo, "update-index", "-q", "--refresh", check=False)
        return self._after(backup, manifest, state, raw, readable, index_lock)

    def _backup_dir(self) -> str:
        ts = time.strftime(_TS_FORMAT, time.gmtime()) + f"-{os.getpid()}"
        return os.path.join(self.backups, ts + SUFFIX)

    def _locks(self) -> list[tuple[str, str]]:
        """(git-path name, absolute path) of each lock a killed switch can leave:
        index.lock mid-write; HEAD.lock and the branch's ref lock at its ref
        transaction (MEASURED, git 2.43); packed-refs.lock."""
        ref = _out(self.repo, "symbolic-ref", "-q", "HEAD", check=False)
        names = ["index.lock", "HEAD.lock", "packed-refs.lock"] + ([f"{ref}.lock"] if ref else [])
        where = _out(self.repo, "rev-parse", "--path-format=absolute",
                     *(a for n in names for a in ("--git-path", n)))  # fmt: skip
        return list(zip(names, where.split("\n"), strict=True))

    def _retire_locks(self, backup: str) -> None:
        """The one retire step for every confirmed-stale git lock (no process
        holds it, by _held_open): copied into the backup's git/, made durable,
        then removed. Called wherever the repair ends healed, before anything
        restores (the restore needs index.lock gone)."""
        stale = [(n, p) for n, p in self._locks() if os.path.lexists(p) and not _held_open(p)]
        for name, path in stale:
            self._report_dir(backup)
            os.makedirs(os.path.dirname(os.path.join(backup, "git", name)), exist_ok=True)
            shutil.copy2(path, os.path.join(backup, "git", name), follow_symlinks=False)
        if stale:
            self._sync(backup)
        for _name, path in stale:
            os.unlink(path)

    def _report_dir(self, backup: str) -> None:
        """deploy-backups and the report dir, this user's alone (owner ruling
        73b6285d): made 0700, and an existing deploy-backups of this user's set
        to 0700 (a symlink or another user's is left for the restore's origin
        check to refuse). The report dir is one this run made: a node already at
        its name that is not a 0700 directory of this user's (a planted link or
        a dir others can write) refuses before a byte is saved, and _sync's
        walk never follows one. What is put below loses group and world write
        in _sync."""
        os.makedirs(self.backups, mode=0o700, exist_ok=True)
        st = os.lstat(self.backups)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077:
            os.chmod(self.backups, 0o700)
            st = os.lstat(self.backups)
        # The restore's origin rule (live_move_restore._anchor) refuses such a
        # root. The repair still saves into it and heals, since refusing would
        # leave the checkout half-written, and says so (owner ruling, #3974 item 6).
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            self.backups_warning = (
                f"{self.backups} is not a folder of this user's alone (a link, another"
                " user's, or writable by others): the backup was saved there, but the"
                " restore check refuses reports under it; move the report into a 0700"
                " deploy-backups of this user's to restore it"
            )
        with contextlib.suppress(FileExistsError):
            os.mkdir(backup, 0o700)
        st = os.lstat(backup)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise RepairError(
                f"{backup} is not a directory of this user's alone; nothing was saved"
            )

    def _keep_index(self, backup: str) -> bool:
        """Copy .git/index byte for byte into the backup before anything
        changes (its locks are kept by _retire_locks). A torn index (its trailing checksum fails: git
        reads one without checking it, then cannot write it back) is rebuilt
        from the tag (HEAD at the tag; in the forward case HEAD is the target,
        whose trees a power loss may have taken); its staged entries cannot be
        read, so its bytes are the record. Returns whether it was rebuilt."""
        index = _out(self.repo, "rev-parse", "--path-format=absolute", "--git-path", "index")
        self._report_dir(backup)
        os.mkdir(os.path.join(backup, "git"))
        if not os.path.lexists(index):
            return False
        shutil.copy2(index, os.path.join(backup, "git", "index"))
        with open(index, "rb") as fh:
            data = fh.read()
        n = hashlib.new(self.fmt).digest_size
        if data[-n:] in (hashlib.new(self.fmt, data[:-n]).digest(), bytes(n)):  # 0s: skipHash
            return False
        self._sync(backup)
        self._retire_locks(backup)
        _git(self.repo, "read-tree", self.tag)
        return True

    def _rows(self, to: str | None, index: dict, flags: dict) -> list[dict]:
        """The rows of the move's paths toward ``to``; with None (the target
        cannot be read), of every change against the tag, in the worktree and
        in the index (in the forward case HEAD is the unreadable target)."""
        repo = self.repo
        self.other = _tree(repo, to) if to else {}
        self.tag_dirs = {d for p in self.want for d in _parents(p)}
        revs = (self.tag, to) if to else (self.tag,)
        paths = set(_z(_git(repo, "diff", "-z", "--no-renames", "--name-only", *revs).stdout))
        if not to:  # staged, flagged and untracked paths included
            cached = ("diff", "-z", "--no-renames", "--name-only", "--cached", self.tag)
            paths |= set(_z(_git(repo, *cached).stdout))
            paths |= set(_z(_git(repo, "ls-files", "-z", "--others", "--exclude-standard").stdout))
            paths |= set(flags)
        # A symlink or non-directory parent of ANY path: its own row (in a tag
        # path's way it must go; a target-only path under it is not in the
        # checkout, and _node never reads or unlinks through it).
        for p in list(paths):
            for d in _parents(p):
                full = os.path.join(repo, d)
                if os.path.islink(full) or (os.path.lexists(full) and not os.path.isdir(full)):
                    paths.add(d)
        # The inventory is the restore's footprint: restoring a row path drops
        # every index entry under it and every index entry above it (a file
        # entry `sub` where the tag has sub/, MEASURED git 2.43), so each such
        # entry is a row of its own.
        paths |= {d for p in list(paths) for d in _parents(p) if d in index}
        paths |= {q for q in index if paths.intersection(_parents(q))}
        rows = [r for r in (self._row(p, index, flags) for p in sorted(paths)) if r]
        # A foreign row's disk step needs its directory back (a gitlink's empty
        # one included): a changed directory row holding one is foreign too.
        held = {d for r in rows if r["class"] == "foreign" and r["disk_changed"]
                for d in _parents(r["path"])}  # fmt: skip
        for r in rows:
            if r["path"] in held and r["disk"] == "dir" and r["disk_changed"]:
                r["class"] = "foreign"
        return rows

    def _row(self, p: str, index: dict, flags: dict) -> dict | None:
        """The path's row when anything about it is not the tag's, else None."""
        node = _node(self.repo, p, self.fmt)
        w, o = self.want.get(p), self.other.get(p)
        entries = index.get(p, [])
        disk_changed = node != _as_disk(w)
        if w is None and p in self.tag_dirs and node in (None, ("dir", "")):
            disk_changed = False  # a tag directory: its files are rows of their own
        if not disk_changed and entries == _as_index(w) and p not in flags:
            return None
        special = _special(os.path.join(self.repo, p)) if node and node[0] == "other" else None
        foreign = (
            p in flags
            or special is not None  # never the switch's: it writes files, links, dirs
            or "160000" in (w or ("",))[:1] + (o or ("",))[:1]
            or entries not in (_as_index(w), _as_index(o))
            or (disk_changed and not self._engine_wrote(p, node, o, w))
        )
        return {
            "path": p,
            "class": "foreign" if foreign else "engine",
            "disk": "absent" if node is None else node[0],
            "disk_changed": disk_changed,
            "tag": w,
            "other": o,
            "index": entries,
            "flag": flags.get(p),
            "special": special,  # a FIFO's, a socket's or a device's type and mode
            "saved_as": None,
            "blobs": [],  # the index entries' blobs, saved (a foreign row's)
            "unsaved": [],  # sockets and devices inside a saved directory
            # Seen beyond this symlinked or non-directory parent: not in the checkout.
            "unreachable_through": _link_parent(self.repo, p),
        }

    def _engine_wrote(self, p: str, node, o, w=None) -> bool:
        """The switch's doing: the target's bytes; a path git unlinked before
        writing; an interrupted write (empty, or a prefix of the bytes the
        switch was writing: the target's, or the tag's when a rollback was
        killed, ``w``); an ephemeral path (matched whole, as the guard does); a
        directory of target files. os.walk lists a directory symlink among the
        subdirectories (and never follows it), so those are judged with the files."""
        if node is None or node == o or re.fullmatch(f"(?:{self.ephemeral})", " " + p):
            return True
        full = os.path.join(self.repo, p)
        if node[0] == "dir":
            return all(
                _node(self.repo, rel, self.fmt) == self.other.get(rel)
                for top, subdirs, files in os.walk(full)
                for name in files + [d for d in subdirs if os.path.islink(os.path.join(top, d))]
                for rel in (os.path.relpath(os.path.join(top, name), self.repo),)
            )
        writing = w if self.forward else o
        if (
            node[0] not in ("100644", "100755")
            or not writing
            or writing[0] not in ("100644", "100755")
        ):
            return False
        with open(full, "rb") as fh:
            have = fh.read()
        blob = _git(self.repo, "cat-file", "blob", writing[1]).stdout
        return len(have) < len(blob) and blob.startswith(have)

    def _save(self, backup: str, manifest: dict) -> None:
        """Copy every node about to change, and make them, the index copies
        _keep_index made and REPAIR.json durable before anything changes: the node (a FIFO,
        socket or device is recorded by type and mode, as it holds no bytes), a
        foreign row's index blobs (the restore drops their only reference, and
        a gc would then delete them), and the new directory's entries up to
        the Genesis home."""
        for older, m in manifests(self.backups):
            # A report a restore holds in flight is never superseded (B2b §7).
            flying = isinstance(m.get("restore"), dict) and m["restore"].get("inflight")
            if m.get("result") is None and not flying and os.path.dirname(older) != backup:
                write_json(older, {**m, "result": "superseded"}, private=True)  # killed partway
        # A directory row is copied whole; a row inside it is saved by that copy.
        whole = {r["path"] for r in manifest["paths"] if r["disk"] == "dir" and r["disk_changed"]}
        saved = _Saved(backup)
        for row in manifest["paths"]:
            if row["class"] == "foreign" and [tuple(e) for e in row["index"]] != _as_index(
                row["tag"]
            ):
                self._save_blobs(backup, row)
            if not row["disk_changed"] or row["disk"] in ("absent", "other"):
                continue
            row["saved_as"] = os.path.join("tree", row["path"])
            if whole.intersection(_parents(row["path"])):
                continue
            src, dst = os.path.join(self.repo, row["path"]), os.path.join(backup, row["saved_as"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            copy = functools.partial(
                _copy_node, unsaved=row["unsaved"], root=self.repo, saved=saved
            )
            if row["disk"] == "dir":
                shutil.copytree(src, dst, symlinks=True, copy_function=copy)
                # copytree makes every symlink itself, never through copy_function.
                for top, dirs, files in os.walk(dst):
                    for name in dirs + files:
                        if os.path.islink(link := os.path.join(top, name)):
                            saved.add(link, {"type": "symlink", "target": os.readlink(link)})
            else:
                copy(src, dst)
        write_json(os.path.join(backup, "SAVED.json"), saved.data(), private=True, indent=None)
        self._sync(backup)
        write_json(os.path.join(backup, "REPAIR.json"), manifest, private=True)
        self._retire_locks(backup)

    def _sync(self, backup: str) -> None:
        """Make the backup durable: its files and directories, then its own
        entry and deploy-backups' (it may be new). On the way, every node below
        the report dir but a symlink (its mode means nothing) loses its group
        and world write bits: copytree's directories and the index and lock
        copies keep the checkout's or the umask's."""
        for top, _subdirs, files in os.walk(backup, topdown=False):
            for f in files:
                full = os.path.join(top, f)
                st = os.lstat(full)
                if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022:
                    os.chmod(full, stat.S_IMODE(st.st_mode) & ~0o022)
                if stat.S_ISREG(st.st_mode):  # a FIFO would block
                    with open(full, "rb") as fh:
                        os.fsync(fh.fileno())
            mode = os.lstat(top).st_mode
            if mode & 0o022:
                os.chmod(top, stat.S_IMODE(mode) & ~0o022)
            _fsync_dir(top)
        _fsync_dir(self.backups)
        _fsync_dir(self.home)

    def _save_blobs(self, backup: str, row: dict) -> None:
        """The blob of each of the row's index entries (a gitlink names a
        commit, not stored here), by object id under index-blobs/. A blob the
        object store already lacks was lost before the repair: nothing to save."""
        for mode, oid, _stage in row["index"]:
            rel = os.path.join("index-blobs", oid)
            if mode == "160000" or rel in row["blobs"]:
                continue
            got = _git(self.repo, "cat-file", "blob", oid, check=False)
            if got.returncode != 0:
                continue
            os.makedirs(os.path.join(backup, "index-blobs"), exist_ok=True)
            with open(os.path.join(backup, rel), "wb") as fh:
                fh.write(got.stdout)
            row["blobs"].append(rel)

    def _apply(self, rows: list[dict], index: dict) -> None:
        global _changed
        _changed = True  # from here a failure may leave the checkout part-restored
        repo = self.repo
        flagged = [r["path"] for r in rows if r["flag"]]
        for opt in ("--no-skip-worktree", "--no-assume-unchanged"):  # one flag per call
            if flagged:
                _git(repo, "update-index", opt, "--", *flagged)
        for row in sorted(rows, key=lambda r: r["path"], reverse=True):  # children first
            w = self.want.get(row["path"])
            full = os.path.join(repo, row["path"])
            if row["disk"] == "dir" and row["disk_changed"]:
                shutil.rmtree(full)
            elif (
                row["disk_changed"]
                and row["disk"] != "absent"
                and (w is None or row["disk"] == "other")
            ):
                os.unlink(full)
        # `git restore` drops an index entry the tag lacks but refuses a path known
        # to neither, and refuses an index path under a named tag file (P/inner
        # under P), which naming P covers (MEASURED, git 2.43).
        # An index entry above a row (a file `sub` where the tag has sub/) is
        # dropped by restoring that row (MEASURED, git 2.43); naming it would
        # restore the whole tag directory, files that are not rows included.
        tag_dirs = {d for p in self.want for d in _parents(p)}
        named = {
            r["path"]
            for r in rows
            if r["path"] in self.want or (r["path"] in index and r["path"] not in tag_dirs)
        }
        spec = sorted(p for p in named if not named.intersection(_parents(p)))
        if spec:
            data = b"".join(os.fsencode(":(literal)" + p) + b"\0" for p in spec)
            _git(repo, "restore", f"--source={self.tag}", "--staged", "--worktree",
                 "--pathspec-from-file=-", "--pathspec-file-nul", data=data)  # fmt: skip
        for row in rows:
            for d in _parents(row["path"]):
                # Never through a symlinked or non-directory parent: that
                # rmdir would act outside the checkout.
                if d not in tag_dirs and not _beyond_link(repo, d):
                    with contextlib.suppress(OSError):
                        os.rmdir(os.path.join(repo, d))

    def _after(self, backup, manifest, state, raw, readable, index_lock) -> int:
        if not readable:
            dirty = _git(self.repo, "status", "--porcelain", "-uall").stdout
            if dirty or os.path.lexists(index_lock):
                return self._finish(backup, manifest, "unresolved: the tree is not clean", 1)
            self._clear(raw)
            return self._finish(backup, manifest, "healed (the target was unreadable)", 0)
        judged = state
        move = state.get("live_move")
        partial = isinstance(move, dict) and move.get("phase") == "partial"
        if partial:
            # The guard calls a "partial" record mixed whatever is on disk; the
            # repair undid what it records, so the tree decides, as for "moving".
            judged = {**state, "live_move": {**move, "phase": "moving"}}
        verdict, reason = self.guard.state_verdict(judged, self.repo)
        if verdict == self.guard.SAFE:
            self._clear(raw)
            return self._finish(backup, manifest, "healed", 0)
        if verdict in (self.guard.GUARDS, self.guard.HOLD):
            # The tree is the tag's on the move's paths; what still guards is not
            # the move's (HOLD: a tracked edit off its paths, the state a person
            # keeps or Dismisses, #3532). The start policy for it is the guard's
            # callers', not this repair's (#3373). GUARDS is unreachable here:
            # HEAD is at the tag (checked above, or moved there by the forward
            # case's compare-and-swap), where the guard's live rule
            # answers only MIXED_TREE, HOLD or SAFE; the arm is kept as a pin in
            # case that rule ever grows one.
            if partial:
                # The kept file must say what the guard just judged, or it reads
                # "partial" (MIXED_TREE) forever and every caller sends the
                # operator back here, a new backup each run.
                self._rewrite(raw, judged)
            kept = f"the move's paths were restored; what still holds: {reason}"
            if self.forward:
                # The guard's HOLD remedy says the update never changed the
                # checkout: here it did, and the repair moved `live` back.
                why = reason.split(". ", 1)[0]
                kept = (
                    "the rollback was finished (the move's paths restored, `live` moved back "
                    f"to the tag); what still holds: {why}. Save that edit if you want it (it "
                    "may be another session's), then Dismiss the update on the dashboard's "
                    "update card and run scripts/update.sh again"
                )
            return self._finish(backup, manifest, f"healed, state kept: {kept}", 0)
        return self._finish(backup, manifest, f"unresolved: {verdict}: {reason}", 1)

    def _rewrite(self, raw: bytes, state: dict) -> None:
        """Replace the state with ``state``, only while it is still the bytes that
        were judged (as _clear)."""
        try:
            same = self.guard.read_state(self.state_path) == raw
        except (OSError, ValueError):
            return
        if not same:
            _log("the update state changed during the repair; it is kept")
            return
        write_json(self.state_path, state)

    def _clear(self, raw: bytes) -> None:
        """Remove the state, only while it is still the bytes that were judged."""
        try:
            same = self.guard.read_state(self.state_path) == raw
        except (OSError, ValueError):
            return
        if not same:
            _log("the update state changed during the repair; it is kept")
            return
        os.unlink(self.state_path)
        _fsync_dir(self.home)

    def _finish(self, backup: str, manifest: dict, result: str, rc: int) -> int:
        if manifest["index_rebuilt"]:
            result += "; the index was torn: rebuilt from the tag, its bytes saved as git/index"
        manifest["result"] = result
        write_json(os.path.join(backup, "REPAIR.json"), manifest, private=True)
        body = f"{result}. The checkout went back to {manifest['tag'][:12]}; what changed is saved in {backup}."
        foreign = sum(1 for r in manifest["paths"] if r["class"] == "foreign")
        if foreign:
            cmd = f"scripts/deploy_candidates repair-move --restore {manifest['ts']}"
            body += (
                f" {foreign} path(s) were not the update's. To see what putting them back"
                f" would write: {cmd} --check. To put them back: {cmd}."
            )
        hooks_pending = manifest.get("hooks") == "pending"
        if hooks_pending:
            body += " The git hooks are not put back yet; the next repair run retries."
        if manifest.get("hooks_note"):
            body += f" {manifest['hooks_note']}"
        if self.backups_warning:
            body += f" WARNING: {self.backups_warning}."
        self._say(body)
        severity = (
            "critical"
            if foreign
            or manifest["index_rebuilt"]
            or rc
            or self.backups_warning
            or hooks_pending
            or manifest.get("hooks_before")
            else "warning"
        )
        self._alert(severity, body, f"live_move_repair:{manifest['ts']}", backup)
        return rc

    def _alert(self, severity: str, body: str, key: str, backup: str) -> None:
        """Queue the alert, its body cut to ALERT_BYTES (saying how much was
        left out and where the full text is). A failure is logged, never
        raised: it must not change the exit code. Whatever the text holds (a
        `%`, a NUL, a lone surrogate), nothing here raises past the except."""
        try:
            data = body.encode("utf-8", "replace")
            if len(data) > ALERT_BYTES:
                tail = " more bytes; the full text is in " + backup + "/REPAIR.json)"
                room = ALERT_BYTES - len(
                    (" ... (" + str(len(data)) + tail).encode("utf-8", "replace")
                )
                kept = data[: max(room, 0)].decode("utf-8", "ignore")
                body = kept + " ... (" + str(len(data) - len(kept.encode())) + tail
            body = body.encode("utf-8", "replace").decode("utf-8").replace("\0", " ")
            p = subprocess.run(
                ["bash", "-c", '. "$1/alert_queue.sh" && queue_alert "$2" live_move_repair "$3" "$4" "$5"',
                 "alert", _LIB, severity, "Genesis repaired a half-written live checkout", body, key],
                capture_output=True,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - an alert never changes the exit code
            _log(f"the alert was not queued ({exc!r})")
            return
        if p.returncode != 0:
            _log(f"the alert was not queued (exit {p.returncode})")

    def _restore_hooks(self, before: str, after: str) -> bool:
        """The engine's restore-hooks, as update.sh's rollback runs it: the
        checkout's own engine (the tag's, now on disk), handed update.lock's fd.
        No timeout: it runs sync-hooks (up to 120 s) outside BOOT_BUDGET_S. No
        --boot caller exists yet; bounding it inside a server start is PR D2's,
        which wires --boot."""
        env = {
            **os.environ,
            "DEPLOY_CANDIDATES_LOCK_FD": str(self.update_fd),
            "DEPLOY_CANDIDATES_EPHEMERAL_RE": _ephemeral(),
        }
        engine = os.path.join(self.repo, "scripts", "deploy_candidates.py")
        p = subprocess.run(
            ["python3", "-I", "-S", engine, "restore-hooks", before, after],
            env=env,
            pass_fds=(self.update_fd,),
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            _log(f"restore-hooks failed (exit {p.returncode}): {p.stderr.strip()[-2000:]}")
        return p.returncode == 0

    def _report(self, path: str) -> dict | None:
        """The REPAIR.json at ``path`` through load_report, the readers' one
        bounded, no-follow way in, pinned to this checkout; None for anything
        it refuses or that is not there."""
        try:
            loaded = load_report(os.path.dirname(path), self.repo)
        except BadReport:
            return None
        return loaded["report"] if loaded else None

    def _pending_hooks(self) -> list[str]:
        """The REPAIR.json path of every report whose hooks are pending."""
        try:
            names = sorted(n for n in os.listdir(self.backups) if n.endswith(SUFFIX))
        except OSError:
            return []
        kept = [n for n in names if not dismiss_marked(os.path.join(self.backups, n))]
        found = [os.path.join(self.backups, n, "REPAIR.json") for n in kept]
        return [p for p in found if (self._report(p) or {}).get("hooks") == "pending"]

    def _retry_hooks(self, path: str) -> None:
        """A forward repair's hooks not yet put back. Superseded once
        `refs/heads/live` itself left that repair's tag (whatever moved it owns
        the hooks now); retried while `live` is there and HEAD is on it; with
        HEAD elsewhere (detached, another branch) left pending for a later run.
        The report is read here, under update.lock, which every other writer of
        REPAIR.json takes too (--dismiss, a restore), and only its `hooks` key
        is changed, so a dismissal or a restore record is never undone. REPAIR.json is
        user-writable, so its commits must be full commit ids before they reach
        restore-hooks' argv; an entry whose are not is left as it is. Read
        through load_report: a report it refuses (another checkout's, a symlink,
        a name the repair never writes, over its caps) is never retried."""
        manifest = self._report(path)
        if manifest is None or manifest.get("hooks") != "pending":
            return
        tag, other = manifest.get("tag"), manifest.get("other")
        if not all(isinstance(c, str) and _COMMIT.fullmatch(c) for c in (tag, other)):
            return
        live = _out(
            self.repo, "rev-parse", "--verify", "-q", "refs/heads/live^{commit}", check=False
        )
        branch = _out(self.repo, "symbolic-ref", "-q", "HEAD", check=False)
        if live != tag:
            hooks = "superseded"
        elif branch != "refs/heads/live":  # HEAD on `live` is then the tag too
            return
        else:
            note = manifest.get("hooks_note")
            hooks = self._put_hooks_back(os.path.dirname(path), manifest, other, tag)
            if manifest.get("hooks_note") != note:  # hooks moved aside: say where
                severity = "critical" if manifest.get("hooks_before") else "warning"
                key = f"live_move_repair:{manifest.get('ts')}:hooks"
                self._alert(severity, manifest["hooks_note"], key, os.path.dirname(path))
            if hooks == "pending":
                return
        write_json(path, {**manifest, "hooks": hooks}, private=True)
        _log(f"the git hooks of {path} are {hooks}")

    def _put_hooks_back(self, backup: str, manifest: dict, before: str, after: str) -> str:
        """restore-hooks from ``before`` to ``after`` (the tag): "ok", or
        "pending" (a later run retries). When the moved-from commit's hook
        sources cannot be read (its objects are gone), no retry can ever
        restore from it, so the tag's hooks are reinstalled instead:
        "reinstalled". ``hooks_note`` then says so, naming hooks-before/ when
        any hook was moved there (``hooks_before``)."""
        if self._restore_hooks(before, after):
            return "ok"
        if self._hook_sources_readable(before) or not self._reinstall_hooks(
            backup, manifest, after
        ):
            return "pending"
        saved = (
            f"; every installed hook that differed from the tag's (a hand-edited one included) "
            f"is kept in {backup}/hooks-before ({', '.join(manifest['hooks_before'])})"
            if manifest.get("hooks_before")
            else ""
        )
        manifest["hooks_note"] = (
            f"The moved-from commit {before[:12]} cannot be read, so the git hooks the tag's "
            f"sync-hooks.sh lists were reinstalled from the tag {after[:12]}{saved}. A hook "
            "only the moved-from commit listed is left installed."
        )
        return "reinstalled"

    def _hook_sources_readable(self, commit: str) -> bool:
        """Can restore-hooks read ``commit``'s scripts/hooks (the commit, its
        trees down there, and every blob in them)?"""
        if _git(self.repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
            return False
        ls = _git(self.repo, "ls-tree", "-r", "-z", "--full-tree", commit, "--", "scripts/hooks",
                  check=False)  # fmt: skip
        if ls.returncode:
            return False
        blobs = [rec.split("\t", 1)[0].split()[2] for rec in _z(ls.stdout)]
        check = _git(self.repo, "cat-file", "--batch-check", check=False,
                     data=b"".join(o.encode() + b"\n" for o in blobs))  # fmt: skip
        return check.returncode == 0 and b" missing" not in check.stdout

    def _reinstall_hooks(self, backup: str, manifest: dict, tag: str) -> bool:
        """Install the tag's git hooks over whatever is installed, through the
        tag's own engine. Each installed regular-file hook the tag's
        sync-hooks.sh lists whose bytes are not the tag's is copied into
        <backup>/hooks-before/ (0600, never overwriting an earlier copy, made
        durable), its name recorded in REPAIR.json's ``hooks_before`` (with a
        note naming the folder), and only then removed; restore-hooks <tag>
        <tag> then has sync-hooks.sh install them fresh (a same-directory temp
        file renamed over, executable) and judges the result as readiness does.
        Between the removal and that install no such hook is installed (seconds;
        a failure there leaves the record, and the next run converges). Installs
        where sync-hooks.sh does, $GIT_COMMON_DIR/hooks: with core.hooksPath
        pointing elsewhere nothing is touched (readiness names that setting).
        The report dir, and hooks-before in it, must be this user's 0700
        directories (the heal's _report_dir rule; the retry passes a dir only
        load_report checked): otherwise nothing is touched."""
        common = _out(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        hooks = os.path.join(common, "hooks")
        # Where git runs hooks from, read without GIT_C's own core.hooksPath
        # override (rev-parse runs no hook).
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        where = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", self.repo, "rev-parse",
             "--path-format=absolute", "--git-path", "hooks"],
            capture_output=True, env=env,
        )  # fmt: skip
        runs = os.fsdecode(where.stdout.strip())
        if where.returncode or os.path.realpath(runs) != os.path.realpath(hooks):
            _log(f"core.hooksPath points git at {runs}; the git hooks were not reinstalled")
            return False
        aside = os.path.join(backup, "hooks-before")
        if not _private_dir(backup) or (os.path.lexists(aside) and not _private_dir(aside)):
            _log(
                f"{backup} is not this user's private report dir; the git hooks were not reinstalled"
            )
            return False
        listed = _git(self.repo, "show", f"{tag}:scripts/hooks/sync-hooks.sh", check=False)
        text = listed.stdout.decode(errors="replace") if listed.returncode == 0 else ""
        moved: list[tuple[str, str]] = []  # (installed name, the copy's name)
        for name in sorted(os.listdir(hooks)) if os.path.isdir(hooks) else []:
            dst = os.path.join(hooks, name)
            if not _sync_listed(text, name) or not stat.S_ISREG(os.lstat(dst).st_mode):
                continue
            entry = _out(self.repo, "ls-tree", tag, "--", f"scripts/hooks/{name}", check=False)
            want = entry.split()[2] if entry.split()[1:2] == ["blob"] else None
            if want is None or want == _out(self.repo, "hash-object", "--no-filters", "--", dst):
                continue
            if not os.path.lexists(aside):
                os.mkdir(aside, 0o700)
            moved.append((name, _copy_private(dst, aside, name)))
        if moved:
            self._sync(backup)  # the only copy of a hand edit, durable before it goes
            prior = manifest.get("hooks_before")
            prior = prior if isinstance(prior, list) else []
            manifest["hooks_before"] = [*prior, *(copy for _, copy in moved)]
            manifest["hooks_note"] = (
                f"The git hooks {', '.join(n for n, _ in moved)} were moved to {aside} to "
                f"reinstall the tag {tag[:12]}'s, and are not reinstalled yet; the next "
                "repair run retries."
            )
            write_json(os.path.join(backup, "REPAIR.json"), manifest, private=True)
        for name, _copy in moved:
            os.unlink(os.path.join(hooks, name))
        return self._restore_hooks(tag, tag)


def _sync_listed(text: str, name: str) -> bool:
    """Is ``name`` one sync-hooks.sh ``text`` lists? Its arrays quote each
    name on a line of its own, and nothing else in it is a quoted hook name
    (pinned against the engine's parser, gate.sync_hook_names, by a test)."""
    return f'"{name}"' in text


def _private_dir(path: str) -> bool:
    """A directory of this user's (never a link) that no one else can enter."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def _copy_private(src: str, folder: str, name: str) -> str:
    """Copy ``src`` into ``folder`` as a new 0600 file, never following or
    replacing a node already there: ``name``, else ``name.2``, ``name.3``...
    Returns the name used."""
    with open(src, "rb") as fh:
        data = fh.read()
    for n in range(1, 1000):
        used = name if n == 1 else f"{name}.{n}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(os.path.join(folder, used), flags, 0o600)
        except FileExistsError:
            continue
        with open(fd, "wb") as out:
            out.write(data)
        return used
    raise RepairError(f"{folder} holds too many copies of {name}")


_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

EXPLAIN = """The repair's steps, by hand, for when it cannot run:
1. Copy aside every path `git diff --name-only <rollback tag> <target>` lists (the
   target is live_move.to, or own_head for a row without a move record; untracked
   files there included) and a .git/index.lock no git process holds.
2. git restore --source=<tag> --staged --worktree -- <those the tag or the index has>;
   delete the others.
3. HEAD on `live` at the target (a rollback killed partway): then
   git update-ref refs/heads/live <tag> <target>. The git hooks the rebuild
   installed are put back by restore-hooks <target> <tag>, which needs update.lock
   held for it, and no by-hand route takes it: once the kit is installed,
   re-run scripts/deploy_candidates repair-move (it retries pending hooks).
4. Once scripts/lib/update_state_guard.py <state> <repo> exits 10, remove the state."""


def _dismiss_unread(backup: str, why: str) -> int:
    """--dismiss for a report no reader can read: DISMISS_MARK, or 1 when the
    dir itself is not one the repair writes (a link, a file, another user's)."""
    try:
        st = os.lstat(backup)
    except OSError as exc:
        _log(
            f"the repair report in {backup} is not read: {why}; nothing was dismissed ({exc.strerror})"
        )
        return 1
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        _log(
            f"the repair report in {backup} is not read: {why}; it is not this user's directory, so nothing was dismissed"
        )
        return 1
    stamp = time.strftime("%FT%TZ", time.gmtime())
    try:
        write_json(
            os.path.join(backup, DISMISS_MARK),
            {"dismissed_at": stamp, "unread": why[:300]},
            private=True,
        )
    except OSError as exc:
        _log(
            f"the repair report in {backup} is not read: {why}; {DISMISS_MARK} cannot be"
            f" written ({exc.strerror}), so nothing was dismissed"
        )
        return 1
    _log(
        f"the repair report in {backup} is not read ({why}); dismissed with {DISMISS_MARK} beside it"
    )
    return 0


def _dismiss(state: str, repo: str, ts: str, wait: int) -> int:
    """--dismiss TS: rewrite REPAIR.json with dismissed_at, only under
    update.lock (review SF-7: a restore rewrites it too, and an unlocked
    load-then-write would lose the other's field); still held after ``wait``,
    nothing is written. A report load_report refuses (unreadable, or recorded
    for another checkout) is dismissed with DISMISS_MARK beside it instead,
    when its dir is a real directory of this user's: REPAIR.json is left as it
    is, and every reader skips the dir."""
    home = os.path.dirname(os.path.abspath(state))
    backup = os.path.join(home, "deploy-backups", ts + SUFFIX)
    try:
        fd = _flock(_update_lock(home), _now() + wait)
    except (OSError, RepairError) as exc:
        _log(f"update.lock cannot be taken ({exc}); nothing was dismissed")
        return 1
    if fd is None:
        _log(
            "update.lock is still held after the wait (an update or a restore runs); nothing was dismissed"
        )
        return 1
    try:
        try:
            loaded = load_report(backup, repo)
        except BadReport as exc:
            return _dismiss_unread(backup, str(exc))
        if loaded is None:
            _log(f"no repair report in {backup}")
            return 1
        stamp = time.strftime("%FT%TZ", time.gmtime())
        report = {**loaded["report"], "dismissed_at": stamp}
        write_json(os.path.join(backup, "REPAIR.json"), report, private=True)
        return 0
    finally:
        os.close(fd)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="live_move_repair.py")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--boot", action="store_true")
    mode.add_argument("--command", action="store_true")
    mode.add_argument("--dismiss", metavar="TS")
    mode.add_argument("--restore", metavar="TS")
    mode.add_argument("--explain", action="store_true")
    ap.add_argument("state", nargs="?")
    ap.add_argument("repo", nargs="?")
    ap.add_argument("--check", action="store_true")  # with --restore: judge only
    ap.add_argument("--wait", type=int)  # 7200 s; 30 s with --restore
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    if args.explain:
        print(EXPLAIN)
        return 0
    ts = args.dismiss if args.dismiss is not None else args.restore
    if not (args.state and args.repo) or (args.check and args.restore is None):
        return 2
    if ts is not None and report_ts(ts + SUFFIX) != ts:  # #3733: never a path from an unchecked ts
        _log(f"{ts!r} is not a repair report's ts")
        return 2
    repo = os.path.abspath(args.repo)
    if args.dismiss is not None:  # REPAIR.json's readers stop reporting a dismissed repair's saves
        return _dismiss(args.state, repo, args.dismiss, 30 if args.wait is None else args.wait)
    wait = args.wait if args.wait is not None else 30 if args.restore is not None else 7200
    try:
        return Repair(args.state, repo, args.boot, wait).run(args.restore, not args.check)
    except Exception as exc:  # noqa: BLE001 - every failure is named; at boot it sets the exit
        if args.boot and not _changed:  # a failed backup changed nothing in the checkout
            _log(f"skipped; nothing was changed ({type(exc).__name__}: {exc})")
            return 0
        if args.restore is not None and not args.check:
            _log(f"the restore failed ({type(exc).__name__}: {exc}); REPAIR.json's `restore`"
                 " block names any part in flight, and a re-run finishes it")  # fmt: skip
            return 1
        _log(f"failed ({type(exc).__name__}: {exc}); the update state is kept")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
