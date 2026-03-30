#!/usr/bin/env python3
"""Manual trigger for inbox monitor — run a single check cycle.

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/inbox_check.py [--dry-run]

Requires genesis.db to exist (run AZ first, or use --dry-run for scanner-only).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


async def main(dry_run: bool = False) -> None:
    from genesis.env import repo_root
    from genesis.inbox.config import load_inbox_config

    config_path = repo_root() / "config" / "inbox_monitor.yaml"
    if not config_path.exists():
        print(f"ERROR: Config not found at {config_path}")
        sys.exit(1)

    config = load_inbox_config(config_path)
    print(f"Config: watch_path={config.watch_path}, interval={config.check_interval_seconds}s")
    print(f"        batch_size={config.batch_size}, model={config.model}, effort={config.effort}")

    if not config.watch_path.is_dir():
        print(f"Watch path does not exist: {config.watch_path}")
        print("Creating it...")
        config.watch_path.mkdir(parents=True, exist_ok=True)

    # Scanner-only dry run
    from genesis.inbox.scanner import detect_changes, scan_folder

    files = scan_folder(config.watch_path, config.response_dir)
    print(f"\nFiles in inbox: {len(files)}")
    for f in files:
        print(f"  {f.name} ({f.stat().st_size} bytes)")

    if dry_run:
        print("\n[DRY RUN] Skipping CC dispatch.")
        if files:
            new, modified = detect_changes(config.watch_path, {}, config.response_dir)
            print(f"Would process: {len(new)} new, {len(modified)} modified")
        return

    # Full check — needs DB + CC
    from genesis.cc.invoker import CCInvoker
    from genesis.cc.session_manager import SessionManager
    from genesis.db.connection import init_db
    from genesis.inbox.monitor import InboxMonitor
    from genesis.inbox.writer import ResponseWriter

    db = await init_db()
    print(f"\nUsing DB: {db.db_path if hasattr(db, 'db_path') else 'default'}")

    invoker = CCInvoker(working_dir=str(Path.home() / "genesis"))
    session_manager = SessionManager(db=db)
    writer = ResponseWriter(watch_path=config.watch_path)

    monitor = InboxMonitor(
        db=db,
        invoker=invoker,
        session_manager=session_manager,
        config=config,
        writer=writer,
    )

    print("\nRunning inbox check...")
    result = await monitor.check_once()
    print("\nResult:")
    print(f"  Items found: {result.items_found}")
    print(f"  New: {result.items_new}")
    print(f"  Modified: {result.items_modified}")
    print(f"  Batches dispatched: {result.batches_dispatched}")
    if result.errors:
        print(f"  Errors: {result.errors}")
    else:
        print("  Errors: none")

    if result.batches_dispatched > 0:
        from genesis.inbox.scanner import RESPONSE_SUFFIX
        responses = list(config.watch_path.glob(f"*{RESPONSE_SUFFIX}"))
        print(f"\nResponse files in {config.watch_path}:")
        for r in sorted(responses):
            print(f"  {r.name}")

    await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manual inbox check")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, no CC dispatch")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
