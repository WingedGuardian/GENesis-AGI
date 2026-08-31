#!/usr/bin/env python3
"""Secure append-only JSONL for hook audit/recovery logs.

Extracted from ``git_discard_guard.py`` (2026-08-30) when a SECOND hook log —
the merge gate's override record — needed the same properties. The mechanics
below are security properties, not conveniences, and a partial adoption is the
failure this module exists to prevent: the override log's first cut took the
credential rule from the discard guard and left the 0600 modes, the umask-free
create and the sidecar lock behind. The log it produced was mode 644.

WHAT THIS GUARANTEES
====================
* **Own-user-only files, with no umask window.** ``os.open`` applies the mode AT
  create time; ``open()`` + ``chmod`` is briefly group/world-readable per the
  process umask. These logs sit in ``~/.genesis`` beside secrets.
* **Serialized appends, per configured path.** The exclusive ``flock`` rides a
  SIDECAR lock file, not the log fh: a maintainer that calls :func:`rewrite`
  replaces the log inode, so a lock held on the log itself would ride the ORPHAN
  inode and a waiter would append to a file nobody reads. The sidecar is never
  replaced. LIMIT, since the relocation below is supported: the sidecar is keyed
  to the path a caller was GIVEN, while data lands on the resolved target — so two
  different paths pointing at one log serialize on two different sidecars. One
  configured path is safe; two names for the same log are not.
* **Bounded waiting.** Acquisition gives up after :data:`LOCK_TIMEOUT_S`. For a
  caller whose return value is a security verdict, an unbounded wait is a
  fail-open bug rather than a latency one — see that constant.
* **A broken maintainer cannot cost the caller its row.** Each maintainer runs in
  its own ``try``. Precisely: the row is already on disk before any maintainer
  runs, so an unisolated raise costs the caller its RETURN VALUE and any
  SUBSEQUENT rows in the same batch — not the row just written. Reordering append
  and maintenance changes nothing observable; the isolation is what matters.
  Retention still runs AFTER the append, so a maintainer's own read sees the row
  just written and a size trim measures the real size; but ordering is the
  secondary safeguard, not the guarantee. The unisolated version is what made the
  override log's first cut a silent, permanent no-op: one row it could not parse
  raised, the write was abandoned, and every later write hit the same row.
* **Failures are reported, never fatal.** Callers on a hook path must not break
  the user's command over a log write, so :func:`append_row` returns ``None``
  instead of raising — but it says so on stderr rather than swallowing in
  silence. A logger that fails quietly is indistinguishable from one that was
  never wired. CAVEAT, so the guarantee is not overstated: the harness discards a
  PreToolUse hook's stderr when that hook exits 0, so on an ALLOWED command the
  notice reaches the hook log rather than the user; on a block it is visible.

WHAT THIS DELIBERATELY DOES NOT DO
==================================
No schema, no field validation, no free-text policy. Those belong to each log's
own writer, which knows its row shape; this module only gets rows to disk safely.

KNOWN RESIDUALS, stated rather than implied
===========================================
* No ``O_NOFOLLOW``: a symlink at the log path is followed. That also keeps a
  deliberately relocated log working, and the threat requires write access to a
  directory inside the user's own home — at which point the attacker has the home.
* No ``fsync``: a crash immediately after a write can lose recent rows.
* :data:`LOG_DIR_MODE` applies only to a directory this module CREATES.
  ``os.makedirs(exist_ok=True)`` does not tighten an existing one, and the real
  ``~/.genesis`` is 0755 on a normal install — so the effective protection for
  contents is the 0600 FILE mode, not the directory. Filenames and sizes in that
  directory are readable by other local users.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable, Sequence

#: Own-user-only. These logs can carry local repo paths, branch names and PR
#: numbers, and they live beside secrets in ``~/.genesis``. See the residual note
#: above: the directory mode binds only when this module creates the directory.
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600

#: How long to wait for the sidecar lock before giving up on the row.
#:
#: This bound is load-bearing, not tidiness. A caller may be a hook that BLOCKS a
#: command, and its block is delivered by RETURNING in time — a hook killed by the
#: harness wall-clock does not return 2, and a non-2 exit lets the guarded command
#: RUN. An unbounded wait here therefore converts a fail-CLOSED gate into a
#: fail-OPEN one for as long as anything holds the lock. MEASURED before this
#: bound existed: another process holding the sidecar hung the merge gate with no
#: verdict until it was killed at 20s, against a 0.00s baseline.
#:
#: Audit logging must never outlive the verdict it describes. A lost row is a gap
#: in a log; a lost verdict is an unguarded merge.
LOCK_TIMEOUT_S = 0.5
_LOCK_POLL_S = 0.02


def open_own(path: str, *, append: bool):
    """Open ``path`` for writing, creating it 0600 with NO umask window.

    ``os.open`` applies the mode at create time, unlike ``open()`` then
    ``chmod`` (briefly world/group-readable per the process umask). A
    PRE-EXISTING looser file is a separate problem — :func:`restrict` tightens
    that.
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, LOG_FILE_MODE)
    return os.fdopen(fd, "a" if append else "w", encoding="utf-8")


def restrict(path: str) -> None:
    """Best-effort chmod to own-user-only; a chmod failure never aborts logging."""
    with contextlib.suppress(OSError):
        os.chmod(path, LOG_FILE_MODE)


def warn(message: str) -> None:
    """One-line stderr notice. Audit paths report failure — see the module docstring."""
    with contextlib.suppress(Exception):
        print(f"[audit-log] {message}", file=sys.stderr)


def rewrite(log_path: str, lines: Sequence[str]) -> None:
    """Atomically replace ``log_path`` with ``lines`` (one trailing newline each).

    For maintainers only, and only while holding the sidecar lock — see
    :func:`append_row`. The temp file is restricted BEFORE the replace, because
    it becomes the log's inode and so its mode becomes the log's mode.

    Resolves through a SYMLINK before replacing. ``os.replace`` swaps the name it
    is given, so replacing the link itself would delete the relocation this module
    documents as supported and silently split the history: the appended row lands
    on the external target, every later row on a new regular file at the old path.
    MEASURED before this resolved: after one retention sweep the path was no
    longer a symlink.
    """
    target = os.path.realpath(log_path)
    tmp = target + ".tmp"
    with open_own(tmp, append=False) as wr:
        wr.write("".join(line.rstrip("\n") + "\n" for line in lines))
    restrict(tmp)
    os.replace(tmp, target)


def new_deadline() -> float:
    """A single lock deadline to share across a batch of :func:`append_row` calls.

    A caller writing N rows must not pay N × :data:`LOCK_TIMEOUT_S`: the bound
    that matters is on the CALLER's total delay before it can return its verdict,
    not on each row. Without this, 4 rows under contention cost 2s and an
    unbounded compound could cost far more — MEASURED at 60 pending rows.

    SCOPE, so this is not read as more than it is: the deadline bounds LOCK
    WAITING only. Maintenance I/O is additional and runs per row — MEASURED at
    0.216s for 4 rows against a full 1MB log, on top of up to LOCK_TIMEOUT_S of
    waiting. Immaterial against the merge gate's budget, but it is not covered.
    """
    return time.monotonic() + LOCK_TIMEOUT_S


def _take_lock(lock_fh, log_path: str, deadline: float | None = None) -> bool:
    """Take the exclusive sidecar lock, or give up by ``deadline``.

    Non-blocking with a short poll rather than a blocking ``flock``; see
    LOCK_TIMEOUT_S for why an unbounded wait here is a security bug.
    """
    if deadline is None:
        deadline = new_deadline()
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                warn(f"{log_path}: lock busy for {LOCK_TIMEOUT_S}s — row dropped")
                return False
            time.sleep(_LOCK_POLL_S)


def append_row(
    log_path: str,
    row: dict,
    *,
    maintain: Sequence[Callable[[str], None]] = (),
    sort_keys: bool = False,
    deadline: float | None = None,
) -> str | None:
    """Append ``row`` as one JSON line, then run ``maintain`` under the same lock.

    Returns the log path on success, ``None`` if the row could not be written —
    an OS error, or the lock still busy at ``deadline``. Both are reported on
    stderr; neither raises, because a caller's return value may be a security
    verdict.

    ``deadline`` (from :func:`new_deadline`) lets a caller writing several rows
    share ONE budget across all of them, so its total delay stays bounded by
    :data:`LOCK_TIMEOUT_S` rather than N × it. Omit it for a single row.
    """
    try:
        # A relative override has an empty dirname — makedirs("") raises.
        os.makedirs(os.path.dirname(log_path) or ".", mode=LOG_DIR_MODE, exist_ok=True)
        lock_path = log_path + ".lock"
        with open_own(lock_path, append=True) as lock_fh:
            restrict(lock_path)  # tighten a pre-existing loose sidecar
            if not _take_lock(lock_fh, log_path, deadline):
                return None
            with open_own(log_path, append=True) as fh:
                restrict(log_path)
                fh.write(json.dumps(row, sort_keys=sort_keys) + "\n")
            for maintainer in maintain:
                try:
                    maintainer(log_path)
                except Exception as exc:  # noqa: BLE001 — one bad maintainer
                    warn(f"{log_path}: maintenance failed ({exc!r})")
    except Exception as exc:  # noqa: BLE001 — the contract is "never raises".
        # NOT just OSError. A caller's return value may be a security verdict, so
        # anything escaping here can convert an allow into a block via a
        # fail-closed wrapper. MEASURED before this widened: a row containing a
        # non-serialisable value raised TypeError straight through a docstring
        # that promised the function returns None instead of raising.
        warn(f"{log_path}: write failed ({exc!r})")
        return None
    return log_path


def trim_by_size(max_bytes: int) -> Callable[[str], None]:
    """Maintainer: when the log exceeds ``max_bytes``, keep as many of the NEWEST
    rows as fit within it — never a fixed fraction of the line count.

    The size backstop every one of these logs needs regardless of any
    content-based retention, because retention can legitimately keep rows it
    cannot interpret (see :func:`prune_by_age`).

    Two floors, both learned by measurement: an oversized OLD row is dropped
    (halving a line count cannot bound a file whose bulk is one row), but the
    NEWEST row is never dropped even if it alone exceeds the bound — see the
    comment on that branch.
    """

    def _trim(log_path: str) -> None:
        if os.path.getsize(log_path) <= max_bytes:
            return
        with open(log_path, encoding="utf-8") as rd:
            lines = rd.readlines()
        # Retain by accumulated BYTES from the newest end, not by line COUNT.
        # Halving the line count does not bound a file whose bulk is in a few
        # rows, and for a single oversized line `lines[len//2:]` is the whole
        # list — MEASURED: a 5043-byte one-row file was left untouched at a
        # 200-byte bound, so the "size backstop" bounded nothing and every later
        # write re-read it. A row that exceeds the budget alone is DROPPED: the
        # bound is the guarantee, and one unbounded row must not defeat it.
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            size = len(line.encode("utf-8"))
            if used + size > max_bytes:
                break
            kept.append(line)
            used += size
        kept.reverse()
        if not kept:
            # FLOOR. Without it, a newest row larger than the bound leaves `kept`
            # empty and this rewrites the log to ZERO — destroying every older row
            # AND the row just appended, while append_row still returns success.
            # MEASURED before this floor: 20 rows / 890 bytes -> 0 bytes, 0 rows.
            # That is strictly worse than the line-count halving it replaced, which
            # lost at most half. A bound that empties the file has destroyed the
            # thing it was bounding, so the row wins and the bound is reported
            # unmet instead of silently enforced.
            kept = lines[-1:]
            warn(
                f"{log_path}: newest row alone exceeds {max_bytes} bytes — keeping it; "
                "the size bound is NOT met"
            )
        rewrite(log_path, kept)

    return _trim


def prune_by_age(days: int, *, ts_key: str = "ts") -> Callable[[str], None]:
    """Maintainer: drop rows whose ``ts_key`` is older than ``days``.

    A row this cannot interpret — unparseable JSON, a missing key, a
    timezone-NAIVE timestamp that cannot be compared against an aware cutoff —
    is **KEPT**, and the fact is reported once per sweep. Dropping it would let
    a log silently destroy the rows it exists to preserve, and raising on it
    (the first cut's behaviour, with the comparison outside the per-row ``try``)
    made ONE bad row permanently disable all future writes while the gate kept
    telling the operator the override was "(logged)". Keeping is the only
    failure direction that loses nothing; :func:`trim_by_size` bounds the file
    if such rows accumulate.

    Rewrites only when a row actually aged out — otherwise every append would pay
    a full read, rewrite and inode replace to change nothing.
    """

    def _prune(log_path: str) -> None:
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
        kept: list[str] = []
        total = 0
        unreadable = 0
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    ts = datetime.datetime.fromisoformat(json.loads(line)[ts_key])
                    stale = ts < cutoff  # raises on a naive ts — caught below
                except Exception:  # noqa: BLE001 — see docstring: KEEP, never drop
                    unreadable += 1
                    kept.append(line)
                    continue
                if not stale:
                    kept.append(line)
        if unreadable:
            warn(f"{log_path}: kept {unreadable} row(s) with an unreadable {ts_key}")
        if len(kept) != total:
            rewrite(log_path, kept)

    return _prune
