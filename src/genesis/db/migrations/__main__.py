"""CLI entry point for running migrations.

Usage:
    python -m genesis.db.migrations --apply     # Run pending migrations
    python -m genesis.db.migrations --dry-run   # Show pending without applying
    python -m genesis.db.migrations --status    # Show applied/pending status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import aiosqlite

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


async def _run(args: argparse.Namespace) -> int:
    from genesis.db.migrations.runner import MigrationRunner

    db_path = Path.home() / "genesis" / "data" / "genesis.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")  # Match connection.BUSY_TIMEOUT_MS
        runner = MigrationRunner(db)

        if args.status:
            status = await runner.status()
            print(json.dumps(status, indent=2))
            return 0

        if args.dry_run:
            results = await runner.run_pending(dry_run=True)
            if not results:
                print("No pending migrations.")
            else:
                print(f"Would apply {len(results)} migration(s):")
                for r in results:
                    print(f"  {r.id}: {r.name}")
            return 0

        if args.apply:
            results = await runner.run_pending()
            if not results:
                print("No pending migrations.")
                return 0

            failed = [r for r in results if not r.success]
            succeeded = [r for r in results if r.success]

            if succeeded:
                print(f"Applied {len(succeeded)} migration(s):")
                for r in succeeded:
                    print(f"  OK: {r.name} ({r.duration_ms}ms)")

            if failed:
                print(f"FAILED {len(failed)} migration(s):")
                for r in failed:
                    print(f"  FAIL: {r.name} — {r.error}")
                return 1

            return 0

    print("No action specified. Use --apply, --dry-run, or --status.", file=sys.stderr)
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Genesis schema migrations")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="Apply pending migrations")
    group.add_argument("--dry-run", action="store_true", help="Show pending without applying")
    group.add_argument("--status", action="store_true", help="Show migration status")
    args = parser.parse_args()

    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
