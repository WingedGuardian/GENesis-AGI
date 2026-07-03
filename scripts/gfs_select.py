#!/usr/bin/env python3
"""Grandfather-Father-Son retention selection for off-site backup snapshots.

Reads COMPLETE-marked dated snapshot stamps (``YYYYMMDDTHHMMSSZ``, one per line) on stdin
and prints the subset to DELETE (one per line). Keeps the newest snapshot per day
(``--daily``), per ISO week (``--weekly``), and per month (``--monthly``). The NEWEST stamp
overall is ALWAYS kept — restore.sh selects the latest COMPLETE snapshot, so it must never be
pruned. An unparseable line is never emitted for deletion (fail-safe keep).

Stdlib only; pure selection (no filesystem, no network). backup.sh feeds it the COMPLETE
stamps and deletes exactly what it prints.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime


def _parse(stamp: str):
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except (ValueError, TypeError):
        return None


def _keep_recent_buckets(valid, key_fn, count, keep) -> None:
    """Add the newest stamp of each of the ``count`` most-recent buckets to ``keep``.

    ``valid`` is ``[(stamp, dt), ...]`` sorted newest-first, so the first stamp seen for a
    bucket key is that bucket's newest.
    """
    if count <= 0:
        return
    newest_in_bucket: dict = {}
    for stamp, dt in valid:
        k = key_fn(dt)
        if k not in newest_in_bucket:
            newest_in_bucket[k] = stamp
    for k in sorted(newest_in_bucket, reverse=True)[:count]:
        keep.add(newest_in_bucket[k])


def select_deletions(stamps, *, daily: int, weekly: int, monthly: int) -> list[str]:
    """Return the subset of ``stamps`` to DELETE per GFS.

    The newest valid stamp is always kept; unparseable stamps are never returned for deletion.
    Only parseable stamps outside every retention bucket are deleted.
    """
    valid = [(s, dt) for s in stamps if (dt := _parse(s)) is not None]
    valid.sort(key=lambda p: p[1], reverse=True)  # newest first
    if not valid:
        return []
    keep = {valid[0][0]}  # the latest COMPLETE — never pruned
    _keep_recent_buckets(valid, lambda dt: dt.date(), daily, keep)
    _keep_recent_buckets(valid, lambda dt: dt.isocalendar()[:2], weekly, keep)
    _keep_recent_buckets(valid, lambda dt: (dt.year, dt.month), monthly, keep)
    return [s for s, _ in valid if s not in keep]


def main() -> int:
    ap = argparse.ArgumentParser(description="GFS retention: print snapshot stamps to delete")
    ap.add_argument("--daily", type=int, default=7)
    ap.add_argument("--weekly", type=int, default=4)
    ap.add_argument("--monthly", type=int, default=6)
    a = ap.parse_args()
    stamps = [ln.strip() for ln in sys.stdin if ln.strip()]
    for s in select_deletions(stamps, daily=a.daily, weekly=a.weekly, monthly=a.monthly):
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
