#!/usr/bin/env python3
"""Entry point for the detached ledger shadow worker (session-manager PR-3).

Spawned by the PreCompact hook after each compaction snapshot:

    python scripts/ledger_shadow_worker.py --session-id <id> \
        --transcript <path> --end-byte <n> [--trigger manual|auto]

Exit code is always 0 unless argument parsing fails — outcomes (including
errors) are recorded in session_ledger_shadow_runs and the call-site
telemetry row, because nothing is attached to read a detached process's
exit status. Uncaught early failures land on stderr, which the hook
redirects to ~/.genesis/session_awareness/ledger_worker_err.log.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--end-byte", type=int, default=None)
    parser.add_argument("--trigger", default="unknown")
    parser.add_argument(
        "--db-path",
        default=None,
        help="genesis.db path (the spawning hook passes its home-anchored "
        "resolution; default falls back to genesis.env)",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="replay the whole transcript in typed-turn windows "
        "(trigger='backfill', cursor untouched; historical tuning data)",
    )
    parser.add_argument("--turns-per-window", type=int, default=None)
    parser.add_argument("--max-windows", type=int, default=None)
    args = parser.parse_args()

    from genesis.session_awareness.ledger_worker import (
        BACKFILL_MAX_WINDOWS,
        BACKFILL_TURNS_PER_WINDOW,
        run_backfill,
        run_ledger_worker,
    )

    if args.backfill:
        outcome = asyncio.run(
            run_backfill(
                args.session_id,
                args.transcript,
                turns_per_window=args.turns_per_window or BACKFILL_TURNS_PER_WINDOW,
                max_windows=args.max_windows or BACKFILL_MAX_WINDOWS,
                db_path=args.db_path,
            )
        )
        print(f"ledger_shadow_worker backfill: {outcome}")
        return
    if args.end_byte is None:
        parser.error("--end-byte is required without --backfill")
    outcome = asyncio.run(
        run_ledger_worker(
            args.session_id,
            args.transcript,
            args.end_byte,
            trigger=args.trigger,
            db_path=args.db_path,
        )
    )
    if outcome.get("status") in ("failed", "timeout"):
        print(f"ledger_shadow_worker: {outcome}", file=sys.stderr)


if __name__ == "__main__":
    main()
