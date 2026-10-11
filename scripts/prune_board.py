#!/usr/bin/env python3
"""Retention prune for the work board's local stores.

Deletes CLOSED open questions (resolved / dropped) older than ``--question-days``
(default 90) together with their block edges, and board events older than
``--event-days`` (default 180). Unverified questions are open work and promotion
pointers (``board_links``) are durable fact, so neither is ever pruned. Invoked
by ``scripts/disk_hygiene.sh`` (the genesis-disk-hygiene timer); also runnable by
hand. Best-effort — a failure here must not skip other hygiene steps.

Mirrors ``scripts/prune_contributor_issue_posts.py``.
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


async def _prune(question_days: int, event_days: int) -> dict | None:
    from genesis.db.connection import get_raw_db
    from genesis.db.crud import board

    now = datetime.now(UTC).isoformat()
    async with get_raw_db() as conn:
        if not await board.tables_available(conn):
            return None
        return await board.prune(conn, now=now, question_days=question_days, event_days=event_days)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--question-days", type=int, default=90, help="closed questions older than this are deleted"
    )
    ap.add_argument(
        "--event-days", type=int, default=180, help="board events older than this are deleted"
    )
    args = ap.parse_args()
    try:
        result = asyncio.run(_prune(args.question_days, args.event_days))
        if result is None:
            print("board prune: tables not migrated yet; nothing to do")
        else:
            print(
                f"board prune: deleted {result['questions']} closed question(s) "
                f"(+{result['question_blocks']} block edge(s)) older than {args.question_days}d, "
                f"{result['events']} event(s) older than {args.event_days}d"
            )
    except Exception as exc:
        print(f"board prune error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
