#!/usr/bin/env python3
"""Entry point for the detached zero-drop stranded-work detector.

Spawned by the SessionStart hook at startup/resume/compact boundaries, and
once a day by ``scripts/disk_hygiene.sh`` (the wall-clock floor, so a box that
starts no sessions still sweeps):

    python scripts/zero_drop_worker.py --trigger session_start \
        [--db-path <genesis.db>] [--repo-path <repo>]

Manual / E2E form (bypasses the global debounce):

    python scripts/zero_drop_worker.py --trigger manual --force

Exit code is always 0 unless argument parsing fails — outcomes (including
errors) land in ``~/.genesis/zero_drop/last_run.json`` and the subsystem
heartbeat, because nothing is attached to read a detached process's exit
status. Uncaught early failures go to stderr, which the spawning hook
redirects to ``~/.genesis/session_awareness/zero_drop_err.log``.
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
    parser.add_argument(
        "--trigger",
        default="manual",
        choices=["session_start", "hygiene", "precompact", "manual"],
    )
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
        "--repo-path",
        default=None,
        help="repository to sweep (default: genesis.env.repo_root)",
    )
    args = parser.parse_args()

    from genesis.session_awareness.zero_drop_worker import run_zero_drop_worker

    outcome = asyncio.run(
        run_zero_drop_worker(
            trigger=args.trigger,
            force=args.force,
            db_path=args.db_path,
            repo_path=args.repo_path,
        )
    )
    print(f"zero_drop_worker: {outcome}")
    if outcome.get("status") in ("failed", "degraded"):
        print(f"zero_drop_worker: {outcome}", file=sys.stderr)


if __name__ == "__main__":
    main()
