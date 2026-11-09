#!/usr/bin/env python3
"""Retention prune for client_tool_calls (the labelled-client tool-call log).

Deletes rows older than a retention window (default
``client_tool_calls.DEFAULT_RETENTION_DAYS``, whose comment says why) so the log
stays bounded. Invoked by ``scripts/disk_hygiene.sh`` (genesis-disk-hygiene.timer);
also runnable by hand. Best-effort: a failure here must not skip other hygiene
steps (it exits nonzero, which hygiene reports and moves past); an absent
database or table prunes nothing and creates nothing.
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
    from genesis.db.connection import connect_aiosqlite_rw
    from genesis.db.crud.client_tool_calls import prune
    from genesis.env import genesis_db_path

    path = genesis_db_path()
    if not path.exists():
        return 0  # nothing to prune, and never create a database to prune
    # existing_only: SQLite mode=rw, so a database removed after the check
    # above is never recreated empty.
    async with connect_aiosqlite_rw(path, existing_only=True) as conn:
        return await prune(conn, older_than_days=days, now=datetime.now(UTC).isoformat())


def main() -> None:
    from genesis.db.crud.client_tool_calls import DEFAULT_RETENTION_DAYS

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help="retention window in days (rows older than this are deleted)",
    )
    args = ap.parse_args()
    try:
        deleted = asyncio.run(_prune(args.days))
        print(f"client_tool_calls prune: deleted {deleted} row(s) older than {args.days}d")
    except Exception as exc:
        print(f"client_tool_calls prune error: {exc}", file=sys.stderr)
        # Nonzero so disk hygiene reports it; hygiene still runs its other steps.
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
