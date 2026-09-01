#!/usr/bin/env python3
"""Append-only audit records for hook logs: one FILE per flush.

Extracted from ``git_discard_guard.py`` when a second hook log — the merge gate's
override record — needed the same properties.

WHY ONE FILE PER FLUSH
======================
An earlier version of this module appended every row to a single shared JSONL and
maintained it in place: an exclusive lock on a sidecar, a tail-terminator check, an
age prune and a size trim that rewrote the file through a temp and an atomic replace.
That design produced five review rounds, and three of them introduced the defect the
next one found — every instance of one rule (hold a verified descriptor, then never
resolve the path again) plus a retention engine whose failure mode was destroying the
data it bounded.

Writing a fresh file instead removes the whole class rather than another instance of
it. There is no shared mutable file, so there is nothing to lock, no tail to inspect,
no inode to replace and no crash window between truncate and write. ``O_CREAT|O_EXCL``
on a name that does not exist yet cannot be redirected: a symlink or FIFO planted at
that name fails with ``EEXIST`` and the next candidate is tried. Retention moves to
``scripts/prune_hook_audit_logs.py`` on the daily ``disk_hygiene.sh`` timer, which is
where every other store in this repo bounds itself, and deleting whole old files is
the one retention shape a per-file store makes safe.

WHAT THIS GUARANTEES
====================
* **Own-user-only, with no umask window.** ``os.open`` applies the mode at CREATE
  time; ``open()`` then ``chmod`` is briefly group/world-readable. These files sit in
  ``~/.genesis`` beside secrets.
* **One resolution by name per flush, and it creates the file.** Nothing reopens it.
* **A batch is one ``os.write``, or as many as the kernel needs to finish it.** All
  rows land or none do, so a multi-sigil command is never half-recorded.
* **Bounded work.** Serialisation happens before any filesystem call, and the row
  count is capped — see :data:`_MAX_ROWS` for why that is a security property here.
* **Failures are reported, never fatal.** A caller on a hook path must not break the
  user's command over an audit write, so every entry point returns ``None``/``0``
  instead of raising, and says so on stderr rather than swallowing in silence.

KNOWN RESIDUALS, stated rather than implied
===========================================
* The DIRECTORY is created with :data:`LOG_DIR_MODE`, but ``os.makedirs`` does not
  apply that mode to intermediate components and does not tighten one that already
  exists. The per-file 0600 mode is what protects contents; whether the directory
  listing is visible to other local users depends on the home above it.
* No ``fsync``. A crash immediately after a write can lose that flush. The previous
  design could lose the entire retained history to the same crash, because the
  atomic replace was what published it.
* Files are named from the wall clock plus pid. Two flushes in the same microsecond
  from one process take the retry suffix; the ordering the pruner relies on is the
  name, so a clock stepped backwards makes a NEW file sort as though it were old.
  The consequence is an out-of-order deletion — including, once the store is over
  its bound, of a recent record that is not yet in a backup. Naming is not a
  correctness boundary for the WRITE (no record is ever overwritten), but it is the
  pruner's only ordering signal, and this is the one case where it can drop the
  wrong one.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import stat
import sys
from collections.abc import Sequence

#: Own-user-only. These records carry local repo paths, branch names and PR numbers,
#: and they live beside secrets in ``~/.genesis``.
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600

#: Distinct names to try before giving up. Only a same-microsecond collision from the
#: same pid consumes one, so this is generous by design.
_NAME_RETRIES = 100

#: Rows accepted in one flush. A caller notes one row per override sigil on one
#: command, so a real flush is one to four.
#:
#: This bound is a security property, not tidiness. A sibling helper on the same hook
#: path once did work proportional to the command's size, and on a long command that
#: cost enough to exceed a shell hook's registration timeout — the hook was killed
#: before its ``exit 2``, and a non-blocking exit lets the refused command RUN.
#: Serialisation here is linear rather than quadratic, but a cap keeps "no unbounded
#: work on a verdict path" true by construction instead of by argument.
_MAX_ROWS = 64


def warn(message: str) -> None:
    """One-line stderr notice. Audit paths report failure — see the module docstring."""
    with contextlib.suppress(Exception):
        print(f"[audit-log] {message}", file=sys.stderr)


def _stamp() -> str:
    """UTC, microsecond-resolution, lexically sortable — the pruner orders by name."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S_%f")


def write_batch(dir_path: str, rows: Sequence[dict], *, sort_keys: bool = False) -> str | None:
    """Write ``rows`` as one new JSONL file in ``dir_path``; return its path or None.

    Serialises everything BEFORE touching the filesystem, so a row carrying a
    non-serialisable value costs nothing on disk rather than leaving a partial file.

    ``O_EXCL`` means the open either creates the file or fails; it can never open
    something already at that name. With ``O_NOFOLLOW`` alongside it, a symlink or a
    FIFO planted at a candidate name yields ``EEXIST`` and the next candidate is
    tried — there is no window in which this writes through a planted RECORD name.

    SCOPE, because the stronger version of that sentence would be false: those flags
    guard the FINAL component only. A symlinked STORE DIRECTORY is still followed —
    ``os.makedirs(exist_ok=True)`` resolves it — so an attacker who can replace the
    store path itself redirects the whole store. That is a strictly larger capability
    than planting one name, and the boundary against it is the permissions on the
    directory above, not this open.

    Never raises. Every failure returns ``None`` after reporting on stderr.
    """
    try:
        if not rows:
            return None
        if len(rows) > _MAX_ROWS:
            warn(f"refusing a batch of {len(rows)} rows (cap {_MAX_ROWS})")
            return None
        payload = b"".join(json.dumps(r, sort_keys=sort_keys).encode() + b"\n" for r in rows)
        os.makedirs(dir_path, mode=LOG_DIR_MODE, exist_ok=True)
        pid = os.getpid()
        stamp = _stamp()
        for n in range(_NAME_RETRIES):
            path = os.path.join(dir_path, f"{stamp}Z-{pid}-{n}.jsonl")
            try:
                fd = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    LOG_FILE_MODE,
                )
            except FileExistsError:
                continue
            try:
                written = 0
                while written < len(payload):
                    # os.write may write fewer bytes than asked; a single call would
                    # truncate the batch silently on a short write.
                    written += os.write(fd, payload[written:])
            except BaseException:
                # A partial record is worse than no record. The store's documented
                # reader is `cat <store>/*.jsonl`, so one truncated final line makes
                # every consumer raise on the WHOLE store — the failure the shared
                # file used a tail-terminator check to survive. Here the record is
                # its own file, so removing it contains the damage to itself and
                # makes "all rows land or none do" true rather than aspirational.
                os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(path)
                raise
            os.close(fd)
            return path
        warn(f"{dir_path}: no free name after {_NAME_RETRIES} attempts — batch dropped")
        return None
    except Exception as exc:  # noqa: BLE001 — the contract is "never raises"
        warn(f"{dir_path}: write failed ({exc!r})")
        return None


def trim_dir_by_size(dir_path: str, max_bytes: int) -> int:
    """Delete the OLDEST files until the directory fits ``max_bytes``; return bytes freed.

    For ``scripts/prune_hook_audit_logs.py`` on the daily timer — never a hook. A
    missing directory is 0, not an error: the store is created on first write and an
    install that has never used an override has no directory.

    The NEWEST file is never deleted, even when it alone exceeds the bound. That is
    what makes this safe to run beside a live writer: a flush in progress is always
    the newest name, so it can never be the deletion candidate. Ordering is by NAME,
    which the timestamp prefix makes chronological.

    Entries that are not regular files are skipped without following them, so a
    symlink planted in the directory is neither read nor unlinked through.
    """
    freed = 0
    try:
        with os.scandir(dir_path) as entries:
            files = []
            for entry in entries:
                if not entry.name.endswith(".jsonl"):
                    continue
                with contextlib.suppress(OSError):
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        files.append((entry.name, entry.path, st.st_size))
        files.sort()  # timestamp-prefixed names sort oldest-first
        total = sum(size for _, _, size in files)
        # The newest is excluded from the CANDIDATE LIST rather than guarded by a
        # counter. An earlier version tracked survivors in a variable decremented
        # inside a suppressed-OSError block, so one failed unlink stopped the count
        # tracking reality and the loop walked past the last element and deleted the
        # newest record — the file a live writer may be mid-write on, and the one
        # least likely to be in a backup yet. A survivor invariant enforced by
        # arithmetic is not an invariant; slicing makes it structural.
        for _name, path, size in files[:-1]:
            if total <= max_bytes:
                break
            try:
                os.unlink(path)
            except OSError:
                continue  # a file we could not remove is not a file we removed
            total -= size
            freed += size
    except FileNotFoundError:
        return 0
    except OSError as exc:
        warn(f"{dir_path}: trim failed ({exc!r})")
    return freed
