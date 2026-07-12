#!/usr/bin/env python3
"""Retention prune for capability_shadow_events (WS5 Discord SHADOW store).

Deletes shadow observations older than a retention window (default 45 days) so
the observe-only capability-gate log stays bounded. Invoked by
``scripts/disk_hygiene.sh`` (the genesis-disk-hygiene.timer); also runnable by
hand. Best-effort — a failure here must not skip other hygiene steps, and it
no-ops cleanly if the table is absent (the table-existence guard returns 0).

Mirrors ``scripts/prune_immunity_shadow.py`` — the two shadow stores share the
same retention shape.
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
    from genesis.db.crud.capability_shadow import prune_capability_shadow_events

    now = datetime.now(UTC).isoformat()
    async with get_raw_db() as conn:
        return await prune_capability_shadow_events(conn, older_than_days=days, now=now)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=45,
        help="retention window in days (rows older than this are deleted)",
    )
    args = ap.parse_args()
    try:
        deleted = asyncio.run(_prune(args.days))
        print(f"capability_shadow prune: deleted {deleted} row(s) older than {args.days}d")
    except Exception as exc:
        print(f"capability_shadow prune error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
