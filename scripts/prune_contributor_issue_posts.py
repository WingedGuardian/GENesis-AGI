#!/usr/bin/env python3
"""Retention prune for pending_issue_posts (Contributor Work-Log hold store).

Deletes TERMINAL rows (posted / rejected / expired / dry_run) older than a
retention window (default 30 days) so the hold store stays bounded. ``held``
rows are NEVER pruned — they await owner action indefinitely. Invoked by
``scripts/disk_hygiene.sh`` (the genesis-disk-hygiene.timer); also runnable by
hand. Best-effort — a failure here must not skip other hygiene steps.

Mirrors ``scripts/prune_capability_shadow.py`` — same retention shape.
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
    from genesis.db.crud.pending_issue_posts import prune_terminal

    now = datetime.now(UTC).isoformat()
    async with get_raw_db() as conn:
        return await prune_terminal(conn, older_than_days=days, now=now)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=30,
        help="retention window in days (terminal rows older than this are deleted)",
    )
    args = ap.parse_args()
    try:
        deleted = asyncio.run(_prune(args.days))
        print(
            f"contributor_issue_posts prune: deleted {deleted} terminal row(s) "
            f"older than {args.days}d"
        )
    except Exception as exc:
        print(f"contributor_issue_posts prune error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
