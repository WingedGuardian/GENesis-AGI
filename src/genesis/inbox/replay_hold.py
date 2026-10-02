"""Operator-only inspection and acknowledged release of an inbox replay hold."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.db.connection import connect_aiosqlite_rw, open_ro_connection
from genesis.db.crud import inbox_items
from genesis.env import db_busy_timeout_ms, genesis_db_path
from genesis.inbox.scanner import compute_hash


async def operate(
    db_path: Path, item_id: str, *, release: bool = False, acknowledge: bool = False,
) -> str:
    """Never dispatch or bootstrap a database; one acknowledged row may be released."""
    if release and not acknowledge:
        raise ValueError("Release requires --acknowledge-replay; actions may run twice.")
    if not db_path.is_file():
        raise ValueError("Existing Genesis database not found; no database was created.")
    reader = await open_ro_connection(db_path)
    try:
        row = await inbox_items.get_by_id(reader, item_id)
    finally:
        await reader.close()
    if not row or row["status"] != "failed" or not str(row["error_message"] or "").startswith(
        inbox_items.REPLAY_UNSAFE_PREFIX
    ):
        raise ValueError("Selected item is not a failed replay-held batch.")
    # Do not echo raw error text or item URLs: they can carry credentials.
    if not release:
        return (
            f"Item {row['id']}: replay-held, retry_count={row['retry_count']}. "
            "Work may already have run; releasing it can repeat actions. "
            "Use --release --acknowledge-replay only after inspection."
        )
    if not inbox_items._handled_items_from_storage(row["batch_items"]):
        raise ValueError("Held item boundaries are unreadable; operator repair is required.")
    try:
        unchanged = compute_hash(Path(row["file_path"])) == row["content_hash"]
    except OSError as exc:
        raise ValueError("Source is missing or unreadable; hold retained.") from exc
    if not unchanged:
        raise ValueError("Source changed since this attempt; hold retained.")
    # Keep canonical pre/post-open quarantine admission, but never create a
    # replacement DB if the inspected file disappears before the writer opens.
    writer = await connect_aiosqlite_rw(db_path, existing_only=True)
    try:
        writer.row_factory = aiosqlite.Row
        await writer.execute(f"PRAGMA busy_timeout={db_busy_timeout_ms()}")
        await writer.execute("PRAGMA foreign_keys=ON")
        changed = await inbox_items.release_replay_hold(
            writer, row, released_at=datetime.now(UTC).isoformat(),
        )
    finally:
        await writer.close()
    if not changed:
        raise ValueError("Item changed concurrently; no release performed.")
    return (
        f"Item {row['id']} released; retry_count reset to zero. "
        "No work dispatched. The next inbox scan uses normal approval and claim handling."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item", required=True, help="Exact inbox_items row ID (no bulk release)")
    parser.add_argument("--db", type=Path, default=None, help="Existing install database")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--acknowledge-replay", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(operate(
            args.db or genesis_db_path(), args.item,
            release=args.release, acknowledge=args.acknowledge_replay,
        ))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Refused: {exc}\n")
    except sqlite3.Error:
        parser.exit(1, "Refused: database operation failed; no release confirmed.\n")
    print(result)
    return 0
