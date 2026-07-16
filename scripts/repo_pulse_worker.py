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
    args = parser.parse_args()

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
