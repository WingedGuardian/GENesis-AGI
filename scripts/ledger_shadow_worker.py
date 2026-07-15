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
    parser.add_argument("--end-byte", type=int, required=True)
    parser.add_argument("--trigger", default="unknown")
    args = parser.parse_args()

    from genesis.session_awareness.ledger_worker import run_ledger_worker

    outcome = asyncio.run(
        run_ledger_worker(
            args.session_id,
            args.transcript,
            args.end_byte,
            trigger=args.trigger,
        )
    )
    if outcome.get("status") in ("failed", "timeout"):
        print(f"ledger_shadow_worker: {outcome}", file=sys.stderr)


if __name__ == "__main__":
    main()
