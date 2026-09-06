#!/usr/bin/env python3
"""Retention prune for zero_drop_findings (the stranded-work detector store).

Deletes RESOLVED findings older than a retention window (default 45 days).
ACKED rows are never pruned at any age: an ack is live suppression state, and
deleting one would silently un-suppress the finding on the next sweep. Open
rows are the working set. Invoked by ``scripts/disk_hygiene.sh`` (the
genesis-disk-hygiene.timer); also runnable by hand. Best-effort — a failure
here must not skip other hygiene steps, and it no-ops cleanly before the
zero_drop migration lands.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


async def _prune(days: int) -> int:
    from genesis.db.connection import get_raw_db
    from genesis.db.crud.zero_drop import prune_zero_drop

    now = datetime.now(UTC).isoformat()
    async with get_raw_db() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='zero_drop_findings'"
        )
        if await cursor.fetchone() is None:
            return 0
        return await prune_zero_drop(conn, older_than_days=days, now=now)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=45,
        help="retention window in days (RESOLVED rows older than this are deleted)",
    )
    args = ap.parse_args()
    if args.days < 1:
        # `--days 0` prunes everything resolved today; a negative window prunes
        # into the future. Both are silent data loss dressed as retention, and
        # argparse accepts them happily. Refuse rather than interpret.
        ap.error(f"--days must be >= 1 (got {args.days}); a window that small deletes live rows")
    try:
        deleted = asyncio.run(_prune(args.days))
        print(f"zero_drop prune: deleted {deleted} resolved row(s) older than {args.days}d")
    except Exception as exc:
        print(f"zero_drop prune error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
