#!/usr/bin/env python3
"""Entry point for the detached repo-pulse worker (session-manager PR-4a).

Spawned by the SessionStart hook at startup/resume/compact boundaries:

    python scripts/repo_pulse_worker.py --trigger session_start \
        [--db-path <genesis.db>]

Manual / E2E form (bypasses the 30-minute global debounce):

    python scripts/repo_pulse_worker.py --trigger manual --force \
        [--lookback-days 7]

Exit code is always 0 unless argument parsing fails — outcomes (including
errors) are recorded in repo_pulse_runs and the call-site telemetry row,
because nothing is attached to read a detached process's exit status.
Uncaught early failures land on stderr, which the hook redirects to
~/.genesis/session_awareness/repo_pulse_err.log.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _print_verification_backlog(db_path: str | None) -> None:
    """The pr_verifications day-one reader: open obligations, oldest merge first.

    Read-only, no worker run, no debounce — usable while the Wave-3 validator
    session (the eventual consumer) does not exist yet. Prints one line per
    open row plus a status histogram; "0 open" with a nonzero closed count
    means the lane is running and everything recent was docs-exempt or
    verified, while an EMPTY histogram means the table is empty or
    pre-migration — two different states, both printed as what they are.
    """
    import asyncio as _asyncio

    from genesis.db.crud import pr_verifications as verif_crud
    from genesis.env import genesis_db_path

    resolved = db_path or str(genesis_db_path())

    async def _read() -> tuple[list[dict], dict]:
        import aiosqlite

        async with aiosqlite.connect(resolved, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            return await verif_crud.list_open(db), await verif_crud.counts(db)

    rows, histogram = _asyncio.run(_read())
    if not histogram:
        print("pr_verifications: no rows (table empty or pre-migration)")
        return
    for row in rows:
        title = (row.get("pr_title") or "").strip()
        print(f"OPEN  PR #{row['pr_number']}  merged {str(row['merged_at'])[:10]}  {title[:80]}")
    print(
        f"pr_verifications: {histogram.get('open', 0)} open, "
        f"{histogram.get('closed', 0)} closed ({resolved})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trigger", default="manual", choices=["session_start", "manual"])
    parser.add_argument(
        "--force",
        action="store_true",
        help="bypass the global min-interval debounce (manual/E2E runs)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="genesis.db path (the spawning hook passes its home-anchored "
        "resolution; default falls back to genesis.env)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="override the cursor-less enumeration window (config default: 7)",
    )
    parser.add_argument(
        "--verification-backlog",
        action="store_true",
        help="list OPEN post-merge verification obligations (oldest merge "
        "first) and exit — no worker run, no debounce, read-only",
    )
    args = parser.parse_args()

    if args.verification_backlog:
        _print_verification_backlog(args.db_path)
        return

    from genesis.session_awareness.repo_pulse_worker import run_pulse_worker

    outcome = asyncio.run(
        run_pulse_worker(
            trigger=args.trigger,
            force=args.force,
            db_path=args.db_path,
            lookback_days=args.lookback_days,
        )
    )
    print(f"repo_pulse_worker: {outcome}")
    if outcome.get("status") in ("failed", "timeout"):
        print(f"repo_pulse_worker: {outcome}", file=sys.stderr)


if __name__ == "__main__":
    main()
