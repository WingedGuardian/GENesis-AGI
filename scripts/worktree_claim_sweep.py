#!/usr/bin/env python3
"""Keep worktree ownership locks honest: take the ones owed, release the stale ones.

Runs daily from ``scripts/disk_hygiene.sh``, immediately BEFORE the reaper, so
the locks the reaper reads are the ones this sweep just decided on rather than
yesterday's. Also runnable by hand.

The sweep does exactly two things, and the second matters more than the first:

  TAKE     lock a worktree holding uncommitted tracked work (`dirty`).
  RELEASE  drop every lock whose release condition has come true.

Releasing is the half that keeps this from becoming a blanket lock. A lock with
no release condition stops the reaper permanently, and 200 of those would quietly
turn off worktree hygiene altogether -- so a lock this tool cannot decide how to
remove is a lock it refuses to create.

The `claim` rule is NOT taken here. Claims are taken at the moment a session
starts working in a worktree, by the hook that watches edits; a daily batch job
has no way to know which session owns what. This sweep only ever RELEASES claims,
which it can decide from the recorded pid alone.

A FOREIGN lock -- someone's ``git worktree lock --reason "do not touch"`` -- is
counted, reported, and otherwise left completely alone.

Usage:
    worktree_claim_sweep.py                 # take what is owed, release what is stale
    worktree_claim_sweep.py --dry-run       # decide and print, change nothing
    worktree_claim_sweep.py --list          # current ownership of every worktree
    worktree_claim_sweep.py --release-only  # never take a lock, only release
    worktree_claim_sweep.py --take-only     # never release a lock, only take

Stdlib only (no genesis package imports), matching worktree_lifecycle.py, so it
runs under the system interpreter when the venv is absent.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "hooks"))

import worktree_claim as wc  # noqa: E402

# The reaper owns the staleness signal and this tool must not invent a second
# one: a claim that released on its own threshold could pin a worktree the reaper
# was never going to touch, or free one it was about to take. Imported rather
# than copied, and both names survive the in-flight rewrite of that module
# (STALE_DAYS is kept there as the conservative back-compat alias).
sys.path.insert(0, str(_HERE))
from worktree_lifecycle import (  # noqa: E402
    STALE_DAYS,
    _last_activity_time,
    _list_worktrees,
    _repo_root,
)


def _log(msg: str) -> None:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def _decide(root: Path, lock: wc.Lock | None, *, now: float) -> tuple[str, str]:
    """What should happen to this worktree: ``(action, reason)``.

    ``action`` is one of ``take-dirty``, ``release``, ``keep``, ``foreign``,
    ``foreign-stale`` or ``none``.
    """
    try:
        idle = now - _last_activity_time(str(root))
    except OSError:
        idle = None

    if lock is not None:
        if lock.foreign:
            label = wc.describe_foreign(lock.raw)
            # A foreign lock on a worktree nothing has touched in the reaper's
            # staleness window is very likely a leak -- a crashed holder that
            # never unlocked. It is still not ours to release, so say so loudly
            # and leave it: an unreleased lock keeps the reaper away from that
            # worktree forever, and silence is how that becomes permanent.
            if idle is not None and idle >= STALE_DAYS * 86400:
                days = idle / 86400
                return (
                    "foreign-stale",
                    f"{label} lock on a worktree idle {days:.0f}d — the reaper will never "
                    "touch it while this lock stands; release by hand if the holder is gone",
                )
            return "foreign", f"{label} lock — left untouched"
        releasable, why = wc.is_releasable(
            lock,
            root,
            idle_seconds=idle,
            stale_seconds=STALE_DAYS * 86400,
        )
        return ("release" if releasable else "keep"), why

    # An Archon worktree is NOT a special case here, and that is a decision with
    # a measurement behind it rather than an omission. It gets the `dirty` rule
    # like any other worktree: a clean one has nothing for the reaper to destroy,
    # and a dirty one is caught below. A dedicated `archon` rule was built and
    # removed because it deadlocked -- Archon's own cleanup runs
    # `git worktree remove`, the lock refuses it, the environment therefore never
    # leaves `status='active'`, and the release condition keyed on that status
    # could never fire. See the module docstring in hooks/worktree_claim.py.
    if wc.has_tracked_changes(root):
        return "take-dirty", "holds uncommitted tracked changes"
    return "none", "clean and unclaimed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="decide and print, change nothing")
    parser.add_argument("--list", action="store_true", help="report ownership, change nothing")
    parser.add_argument("--release-only", action="store_true", help="never take a lock")
    parser.add_argument("--take-only", action="store_true", help="never release a lock")
    args = parser.parse_args()

    mode = wc.effective_mode()
    if mode == "off" and not args.list:
        _log("worktree_claim_sweep: disabled (mode=off) — no locks taken or released")
        return 0

    repo_root = _repo_root()
    worktrees = _list_worktrees(repo_root)
    archon_paths = wc.archon_active_paths()
    now = time.time()

    _log(
        f"worktree_claim_sweep: {len(worktrees)} worktree(s), "
        f"Archon reports {len(archon_paths)} active environment(s)"
    )

    counts: dict[str, int] = {}
    for entry in worktrees:
        root = Path(entry["path"])
        if not root.exists():
            continue
        lock = wc.read_lock(root)
        action, why = _decide(root, lock, now=now)
        counts[action] = counts.get(action, 0) + 1

        if action == "foreign-stale":
            _log(f"  LEAK? {root.name}: {why}")

        if args.list:
            state = "unlocked" if lock is None else (lock.rule or "foreign")
            _log(f"  [{state:>8}] {root.name}: {why}")
            continue

        if action == "release" and not args.take_only:
            if args.dry_run:
                _log(f"  WOULD RELEASE {root.name}: {why}")
            elif wc.unlock_worktree(root):
                _log(f"  RELEASED {root.name}: {why}")
            else:
                _log(f"  release FAILED {root.name}: {why}")
        elif action.startswith("take-") and not args.release_only:
            rule = wc.RULE_DIRTY
            payload = wc.build_payload(rule)
            if payload is None:
                _log(f"  cannot build a {rule} payload for {root.name} — skipped")
                continue
            if args.dry_run:
                _log(f"  WOULD LOCK {root.name} ({rule}): {why}")
            elif wc.lock_worktree(root, payload):
                _log(f"  LOCKED {root.name} ({rule}): {why}")
            else:
                _log(f"  lock FAILED {root.name} ({rule}): {why}")

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"
    _log(f"worktree_claim_sweep: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
