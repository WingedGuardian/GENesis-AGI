#!/usr/bin/env python3
"""Retention prune for ego_proposal_revisions (PR-5 reconcile revision audit).

Deletes proposal-revision audit rows older than a retention window (default
from the ego_reconcile config's ``revision_retention_days``, falling back to
45 days) so the audit table stays bounded. Invoked by
``scripts/disk_hygiene.sh`` (the genesis-disk-hygiene.timer); also runnable by
hand. Best-effort — a failure here must not skip other hygiene steps, and it
no-ops cleanly if the table is absent (the table-existence guard returns 0).
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


async def _prune(days: int | None) -> int:
    from genesis.db.connection import get_raw_db
    from genesis.db.crud.ego import prune_proposal_revisions
    from genesis.ego import reconcile_config

    if days is None:
        cfg = reconcile_config.load_config()
        days = reconcile_config.knob_int(cfg, "revision_retention_days")
    now = datetime.now(UTC).isoformat()
    async with get_raw_db() as conn:
        return await prune_proposal_revisions(conn, older_than_days=days, now=now)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=None,
        help="retention window in days; default reads ego_reconcile config",
    )
    args = ap.parse_args()
    try:
        deleted = asyncio.run(_prune(args.days))
        print(f"ego_proposal_revisions prune: deleted {deleted} row(s)")
    except Exception as exc:
        print(f"ego_proposal_revisions prune error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
