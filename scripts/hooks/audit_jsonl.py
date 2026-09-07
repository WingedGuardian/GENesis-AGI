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
* **Two names per flush, each resolved exactly once, and neither is ever reopened.**
  The bytes go to a STAGING name (:data:`_STAGING_SUFFIX`), and the record is then
  PUBLISHED by hard-linking that inode to its final ``.jsonl`` name. ``link`` rather
  than ``rename`` because link fails ``EEXIST`` on a taken name instead of replacing
  what is there, which keeps the create-or-fail property the retry loop rests on.
* **A batch is one ``os.write``, or as many as the kernel needs to finish it.** All
  rows land or none do — for a CONCURRENT READER as well as for the writer, which is
  what the staging name buys: the ``*.jsonl`` glob that ``cat`` and ``backup.sh``
  both use can only ever match a complete record, and a mid-write kill leaves a
  scrap the pruner reaps rather than a truncated line that breaks the whole store.
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
* Publishing needs HARD LINKS. On a store directory placed (via the env knob) on a
  filesystem that does not support them, every write fails — loudly, with a stderr
  warning and ``None``, never silently. That is deliberate: the alternative,
  falling back to ``rename``, would silently REPLACE whatever sits at the final
  name, so the degraded mode would destroy records instead of refusing to write
  them. The default store is an ordinary directory under ``~/.genesis``.
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
import time
from collections.abc import Sequence

#: Own-user-only. These records carry local repo paths, branch names and PR numbers,
#: and they live beside secrets in ``~/.genesis``.
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600

#: Distinct names to try before giving up. Only a same-microsecond collision from the
#: same pid consumes one, so this is generous by design.
_NAME_RETRIES = 100

#: Suffix of the STAGING name a batch is written to before it is published.
#:
#: Deliberately not ``.jsonl``: the store's documented reader is
#: ``cat <store>/*.jsonl`` and ``backup.sh`` globs the same pattern, so a name the
#: glob matches is a PUBLISHED record by definition. Writing under this name and
#: publishing afterwards is what makes "all rows land or none do" true for a
#: CONCURRENT READER as well as for the writer — opening the final name first
#: exposes an empty file for the length of the write, and a mid-write SIGKILL
#: leaves a truncated line there permanently, which makes the whole store
#: unparseable (CodeRabbit Major + Codex P2, PR #1609).
#:
#: The cost of hiding a scrap from the glob is that it is also hidden from the
#: pruner, which would be a new unbounded store — so ``trim_dir_by_size`` reaps
#: stale scraps, under the same recency rule it applies to records.
#:
#: TWO limits of that reaping, stated because "unconditionally reaped" would be too
#: strong. Scrap bytes are NOT added to the size total, so the byte bound bounds the
#: records only and a store can sit somewhat over its nominal bound while live
#: scraps exist — bounded by the reap rather than by the trim. And a scrap that is
#: not a REGULAR file is skipped, exactly as a record is: neither is read nor
#: unlinked through, which is the property that matters more than reaping every
#: name. The grace window protects a STALLED writer, not structurally; a flush is
#: milliseconds, and the failure mode if a scrap is taken anyway is a reported
#: ``None`` (the publish raises ENOENT), never a record that silently vanishes.
_STAGING_SUFFIX = ".part"

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
            stem = f"{stamp}Z-{pid}-{n}"
            path = os.path.join(dir_path, f"{stem}.jsonl")
            staging = os.path.join(dir_path, f"{stem}{_STAGING_SUFFIX}")
            try:
                fd = os.open(
                    staging,
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
                # Disown BEFORE closing, not after. Linux frees the descriptor even
                # when close(2) reports an error, so a handler that saw `fd >= 0`
                # would close it a second time and raise EBADF over whatever the
                # real failure was.
                _fd, fd = fd, -1
                os.close(_fd)
                # PUBLISH. `link` rather than `rename` on purpose: rename REPLACES
                # whatever sits at the destination, which would silently destroy an
                # existing record and throw away the create-or-fail property the
                # retry loop is built on. `link` fails EEXIST instead, so a taken
                # name — a real record, or a symlink or FIFO someone planted — sends
                # us to the next candidate exactly as the old direct open did, and
                # nothing is ever written or linked THROUGH a planted name.
                os.link(staging, path)
            except FileExistsError:
                # The final name was taken between the staging open and the link.
                # Drop this scrap and try the next candidate rather than leaving it
                # for the pruner to reap later.
                if fd >= 0:
                    os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(staging)
                continue
            except BaseException:
                # A partial record is worse than no record. Because the bytes went to
                # a staging name the `*.jsonl` glob does not match, no reader ever saw
                # this — cleanup here is tidiness, and the SIGKILL case (where this
                # handler never runs) is covered by the pruner's scrap reaping rather
                # than by hoping the handler gets to run.
                if fd >= 0:
                    os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(staging)
                raise
            # The scrap has served its purpose; the record now lives under its own
            # name. A failure to unlink here costs a reapable scrap, never the record.
            with contextlib.suppress(OSError):
                os.unlink(staging)
            return path
        warn(f"{dir_path}: no free name after {_NAME_RETRIES} attempts — batch dropped")
        return None
    except Exception as exc:  # noqa: BLE001 — the contract is "never raises"
        warn(f"{dir_path}: write failed ({exc!r})")
        return None


#: Every audit store this module backs: env knob -> path under ``~/.genesis``.
#: ONE table, because five consumers were each re-deriving the answer and three of
#: them derived it differently (Codex P2 x3, PR #1609).
STORES = {
    "GENESIS_MERGE_OVERRIDE_DIR": "merge_overrides",
    "GENESIS_DISCARD_SNAPSHOT_DIR": "git_discard_snapshots",
}


def resolve_store_dir(env_var: str) -> str:
    """Where a store lives — the SINGLE answer, for every reader and writer.

    The rule itself is unchanged: an ABSOLUTE override is honoured, a RELATIVE one
    is refused in favour of the default because it would resolve against the hook's
    cwd (the repo) and put durable audit files inside the working tree, where they
    can be committed.

    What changes is that there is now one copy of it. The rule was written out in
    both guards, a THIRD time in the pruner WITHOUT the absolute-path refusal, and
    the default was hardcoded a fourth and fifth time in backup.sh and restore.sh.
    The consequences were not cosmetic: the pruner accepted a relative value and
    trimmed an unrelated directory under its cwd while the real store grew unbounded,
    and an install using a custom store had its audit trail silently excluded from
    backup and restore. A rule that must be REMEMBERED at five call sites is a rule
    that is already wrong at one of them.

    Exposed to the shell too — ``python3 scripts/hooks/audit_jsonl.py --store-dir
    <ENV_VAR>`` — so backup.sh and restore.sh ask rather than assume.
    """
    override = os.environ.get(env_var)
    if override and os.path.isabs(override):
        return override
    if override:
        warn(f"{env_var} must be an absolute path; ignoring {override!r}")
    return os.path.expanduser(f"~/.genesis/{STORES[env_var]}")


#: How recently a file must have been touched to be treated as possibly still
#: being written. A flush is milliseconds; 300s is ~5 orders of magnitude of head
#: room, chosen because the two errors are not symmetric — retaining a little
#: extra costs bytes, deleting a live writer's file costs the only copy of a
#: record.
_ACTIVE_WRITER_GRACE_S = 300


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
        cutoff = time.time() - _ACTIVE_WRITER_GRACE_S
        with os.scandir(dir_path) as entries:
            files = []
            scraps = []
            for entry in entries:
                if entry.name.endswith(_STAGING_SUFFIX):
                    with contextlib.suppress(OSError):
                        st = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(st.st_mode) and st.st_mtime <= cutoff:
                            # `st_nlink` decides whether unlinking this RECLAIMS the
                            # bytes. A scrap whose post-publish unlink failed shares
                            # its inode with the published record, so removing the
                            # scrap frees nothing — counting its size would put a
                            # number in the timer's journal that is simply false, in
                            # the one line an operator reads to see whether retention
                            # is working.
                            scraps.append((entry.path, st.st_size if st.st_nlink <= 1 else 0))
                    continue
                if not entry.name.endswith(".jsonl"):
                    continue
                with contextlib.suppress(OSError):
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        files.append((entry.name, entry.path, st.st_size))
        # Reap abandoned STAGING scraps unconditionally, before any size decision.
        # A scrap is never an audit record — it is what a writer killed mid-batch
        # left behind — and it is invisible to `*.jsonl`, so nothing else would ever
        # remove it. Not gated on the size bound: an unbounded store of scraps is
        # precisely the leak the staging name would otherwise introduce. Anything
        # inside the grace window is skipped above: that is a LIVE writer's scrap.
        for path, size in scraps:
            try:
                os.unlink(path)
            except OSError:
                continue
            freed += size
        files.sort()  # timestamp-prefixed names sort oldest-first
        total = sum(size for _, _, size in files)
        # The newest is excluded from the CANDIDATE LIST rather than guarded by a
        # counter. An earlier version tracked survivors in a variable decremented
        # inside a suppressed-OSError block, so one failed unlink stopped the count
        # tracking reality and the loop walked past the last element and deleted the
        # newest record — the file a live writer may be mid-write on, and the one
        # least likely to be in a backup yet. A survivor invariant enforced by
        # arithmetic is not an invariant; slicing makes it structural.
        # Excluding only the newest name is not enough when two flushes OVERLAP.
        # The names are timestamp-prefixed, so the later flush creates the newer
        # name — and the earlier writer can still have its file open. That earlier
        # file is then an ordinary deletion candidate, and unlinking it leaves the
        # writer returning a path that no longer exists as a successful write
        # (Codex P2, PR #1609).
        #
        # So recency, not just position: nothing modified within the grace window is
        # a candidate, however many newer names exist. The window is generous
        # because the cost is asymmetric — keeping a few extra KB for a minute is
        # nothing, and deleting a live writer's file loses an audit record that
        # exists nowhere else yet. (``cutoff`` is the one computed above, so records
        # and staging scraps are judged against the SAME instant — two reads of the
        # clock could put a file on either side of the window depending on which
        # loop reached it.)
        for _name, path, size in files[:-1]:
            if total <= max_bytes:
                break
            try:
                if os.stat(path).st_mtime > cutoff:
                    continue  # may be a live writer's file
            except OSError:
                continue
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


def _main(argv: list[str] | None = None) -> int:
    """``--store-dir <ENV_VAR>`` — print where that store lives, and exit.

    A module that is otherwise a library, given one CLI verb, so backup.sh and
    restore.sh can ASK where the store is instead of hardcoding the default.
    They hardcoded it, which meant an install with a custom
    ``GENESIS_MERGE_OVERRIDE_DIR`` had its audit trail silently excluded from the
    backup and restored to the wrong place — the trail lost on exactly the rebuild
    it exists for (Codex P2, PR #1609).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--store-dir" and args[1] in STORES:
        print(resolve_store_dir(args[1]))
        return 0
    print(
        f"usage: {__file__} --store-dir <{'|'.join(sorted(STORES))}>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main())
