#!/usr/bin/env python3
"""Retention prune for repo_pulse_runs/_annotations (PR-4a pulse store).

Deletes pulse worker runs + PR↔ledger annotations older than a retention
window (default 45 days) so the annotator store stays bounded. Invoked by
``scripts/disk_hygiene.sh`` (the genesis-disk-hygiene.timer); also runnable
by hand. Best-effort — a failure here must not skip other hygiene steps,
and it no-ops cleanly before migration 0062 lands (the table-existence
guard returns 0).
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


async def _prune(days: int, verification_days: int) -> tuple[int, int]:
    from genesis.db.connection import get_raw_db
    from genesis.db.crud.pr_verifications import prune_closed
    from genesis.db.crud.repo_pulse import prune_repo_pulse

    now = datetime.now(UTC).isoformat()
    async with get_raw_db() as conn:
        pulse_deleted = await prune_repo_pulse(conn, older_than_days=days, now=now)
        # pr_verifications retention (issue #1718 half B): CLOSED rows only —
        # an OPEN row IS the obligation and is never pruned; deleting one would
        # silently forgive an unverified merge. Separate, longer window than
        # the runs/annotations telemetry: closed rows are the audit trail behind
        # "no row within the window means the lane never recorded that PR" — a
        # claim the lane earns by FAILING its run rather than advancing the
        # shared cursor past PRs it could not write.
        verif_deleted = await prune_closed(conn, older_than_days=verification_days, now=now)
        return pulse_deleted, verif_deleted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=45,
        help="retention window in days (rows older than this are deleted)",
    )
    ap.add_argument(
        "--verification-days",
        type=int,
        default=180,
        help="retention for CLOSED pr_verifications rows (open rows are the "
        "obligation and are never pruned)",
    )
    args = ap.parse_args()
    try:
        pulse_deleted, verif_deleted = asyncio.run(_prune(args.days, args.verification_days))
        # Two windows, two numbers: one total against one window would assert a
        # denominator neither count actually has.
        print(
            f"repo_pulse prune: deleted {pulse_deleted} run/annotation row(s) "
            f"older than {args.days}d; {verif_deleted} closed verification row(s) "
            f"older than {args.verification_days}d"
        )
    except Exception as exc:
        print(f"repo_pulse prune error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
