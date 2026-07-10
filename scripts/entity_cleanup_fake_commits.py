"""Remove fake ``commit`` entities created by the pre-fix SHA regex.

The E3 anchor pattern accepted any 7-40 char [0-9a-f] run containing a
digit, so plain numeric IDs (tickets, builds, zero-padded counters like
``000000001``) minted ``commit`` entities during backfill and live
stores. The fixed regex requires ≥1 hex letter as well; this script
deletes every commit entity whose norm_name fails the fixed pattern,
plus its mentions and links.

Dry-run by default; ``--apply`` to delete. Read-only connection is
refused for apply (obviously). Idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite  # noqa: E402

from genesis import env as genesis_env  # noqa: E402
from genesis.db.crud.entities import delete_entities_cascade  # noqa: E402
from genesis.memory.entity_anchors import _SHA_RE  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete (default: dry-run)")
    parser.add_argument("--db", default=None, help="db path override (tests)")
    args = parser.parse_args()

    db_path = args.db or genesis_env.genesis_db_path()
    db = await aiosqlite.connect(db_path)
    try:
        rows = await db.execute_fetchall(
            "SELECT entity_id, norm_name FROM entities WHERE entity_type = 'commit'",
        )
        fake = [
            (eid, norm) for eid, norm in rows if not _SHA_RE.fullmatch(norm)
        ]
        print(f"commit entities: {len(rows)} total, {len(fake)} fake")
        for _eid, norm in fake[:10]:
            print(f"  fake: {norm}")
        if len(fake) > 10:
            print(f"  ... and {len(fake) - 10} more")
        if not fake:
            return 0
        if not args.apply:
            print("dry-run — pass --apply to delete")
            return 0

        ids = [eid for eid, _ in fake]
        # All genesis.db writes go through the crud layer (structural
        # rule) — the cascade lives in crud.entities, testable there.
        counts = await delete_entities_cascade(db, ids)
        print(
            f"deleted: {counts['entities']} entities, "
            f"{counts['mentions']} mentions, {counts['links']} links"
        )
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
