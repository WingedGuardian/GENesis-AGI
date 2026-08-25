#!/usr/bin/env python3
"""Retention prune for entity_merge_journal (PR-1 reversibility snapshot store).

Deletes merge-journal rows older than a retention window (default 180 days) so
the unbounded journal stays bounded. The window is generous by design — the
journal is the substrate for ``unmerge_entity``, so it must outlive the
"did we mis-merge?" discovery window, not just an audit horizon.

Invoked by ``scripts/disk_hygiene.sh`` (the genesis-disk-hygiene.timer); also
runnable by hand. Best-effort — a failure here must not skip other hygiene
steps, and it no-ops cleanly before migration 0086 lands (table-existence guard).
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
    from genesis.db.crud.entities import prune_merge_journal

    now = datetime.now(UTC).isoformat()
    async with get_raw_db() as conn:
        return await prune_merge_journal(conn, older_than_days=days, now=now)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=180,
        help="retention window in days (rows older than this are deleted)",
    )
    args = ap.parse_args()
    try:
        deleted = asyncio.run(_prune(args.days))
        print(f"entity_merge_journal prune: deleted {deleted} row(s) older than {args.days}d")
    except Exception as exc:
        print(f"entity_merge_journal prune error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
