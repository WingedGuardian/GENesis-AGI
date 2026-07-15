#!/usr/bin/env python3
"""CLI shim for the origin_class Qdrant backfill (WS-3 B0).

The implementation lives in ``genesis.memory.origin_class_backfill`` and is
also run automatically by data-migration ``d0001`` on boot — this script is
the manual/one-off entry point (and how the backfill was originally shipped).

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/backfill_origin_class_qdrant.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from genesis.env import genesis_db_path  # noqa: E402
from genesis.memory.origin_class_backfill import backfill_origin_class  # noqa: E402
from genesis.qdrant.collections import get_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        totals = backfill_origin_class(db, get_client(), dry_run=args.dry_run)
    finally:
        db.close()
    verb = "would update" if args.dry_run else "updated"
    print(f"Done: {verb} {sum(totals.values())} points — {totals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
