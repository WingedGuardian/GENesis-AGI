#!/usr/bin/env python3
"""One-time backfill: import legacy charter.json files into session_charters.

PR-1 (#1037) wrote charters as ~/.genesis/sessions/<sid>/charter.json; PR-2a
(migration 0058) makes the DB canonical. This imports every legacy file via
INSERT OR IGNORE — a re-run, or a run after the MCP tools have already edited
a session's DB row, changes nothing (origin immutability and living-field
edits both survive). Legacy charter.json files are LEFT IN PLACE: they are
the injector's fallback if this deploy is ever rolled back.

Also regenerates each imported session's charter.md from the DB row so the
mirror picks up the current renderer format.

Usage:
    python scripts/backfill_session_charters.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill session charters into the DB")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    args = parser.parse_args()

    import aiosqlite

    from genesis.db.crud import session_charters as crud
    from genesis.session_charter import write_charter_md

    db_override = os.environ.get("GENESIS_DB_PATH")
    db_path = Path(db_override) if db_override else Path.home() / "genesis" / "data" / "genesis.db"
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    sessions_override = os.environ.get("GENESIS_SESSIONS_DIR")
    sessions_dir = (
        Path(sessions_override) if sessions_override else Path.home() / ".genesis" / "sessions"
    )

    async def _run() -> int:
        imported = already = invalid = 0
        async with aiosqlite.connect(str(db_path), timeout=5) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='session_charters'"
            )
            if await cursor.fetchone() is None:
                print(
                    "ERROR: session_charters table missing — run migrations first"
                    " (restart genesis-server or `python -m genesis.db.migrations`)",
                    file=sys.stderr,
                )
                return 1

            for charter_file in sorted(sessions_dir.glob("*/charter.json")):
                session_id = charter_file.parent.name
                try:
                    charter = json.loads(charter_file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    print(f"  invalid {charter_file}: {exc}")
                    invalid += 1
                    continue
                origin_prompt = charter.get("origin_prompt")
                if not origin_prompt:
                    print(f"  invalid {charter_file}: no origin_prompt")
                    invalid += 1
                    continue
                if args.dry_run:
                    row = await crud.get(db, session_id)
                    if row is None:
                        print(f"  would import {session_id}")
                        imported += 1
                    else:
                        already += 1
                    continue
                created = await crud.import_charter(
                    db,
                    session_id=session_id,
                    origin_prompt=str(origin_prompt),
                    origin_ts=charter.get("origin_ts"),
                    transcript_path=charter.get("transcript_path"),
                    mission=charter.get("mission"),
                    pointers=charter.get("pointers") or [],
                    compaction_count=int(charter.get("compaction_count", 0)),
                    created_at=charter.get("created_at"),
                    updated_at=charter.get("updated_at"),
                )
                if created:
                    imported += 1
                    print(f"  imported {session_id}")
                else:
                    already += 1
                # Regenerate the mirror from the (possibly pre-existing) DB row
                row = await crud.get(db, session_id)
                if row is not None:
                    ledger = await crud.ledger_list(db, session_id)
                    write_charter_md(sessions_dir, session_id, row, ledger)

        mode = "DRY RUN — " if args.dry_run else ""
        print(f"{mode}imported {imported}, already-in-db {already}, invalid {invalid}")
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
