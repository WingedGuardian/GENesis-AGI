#!/usr/bin/env python3
"""Entry point for the detached ambient worker (WS-C PR2).

Spawned by the proactive memory hook on a drift-trigger fire:

    python scripts/ambient_awareness_worker.py --session-id <id> --no-arbiter

Exit code is always 0 unless argument parsing fails — outcomes
(including errors) are recorded in the session's ambient_verdict.json
and the shadow log, because nothing is attached to read a detached
process's exit status.
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
    parser.add_argument(
        "--no-arbiter",
        action="store_true",
        help="Skip the arbiter stage (PR2 shadow mode; arbiter lands in PR3)",
    )
    args = parser.parse_args()

    from genesis.session_awareness.worker import run_worker

    asyncio.run(run_worker(args.session_id, no_arbiter=args.no_arbiter))


if __name__ == "__main__":
    main()
