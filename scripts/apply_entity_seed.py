#!/usr/bin/env python3
"""Apply the curated entity-layer seed to the live DB (idempotent).

Usage:
    python scripts/apply_entity_seed.py [--db PATH]

Prints counts and the OMI→repo-split spine as a spot-check.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


async def main() -> None:
    import aiosqlite

    from genesis import env as genesis_env
    from genesis.db.crud import entities as entities_crud
    from genesis.memory.entity_seed import apply_seed

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    db_path = args.db or str(genesis_env.genesis_db_path())

    db = await aiosqlite.connect(db_path)
    try:
        counts = await apply_seed(db)
        print(f"seed applied to {db_path}: {counts}")

        omi = await entities_crud.get_by_norm_name(db, norm_name="omi")
        if omi:
            reached = await entities_crud.connected_entities(db, [omi["entity_id"]])
            names = {}
            for eid, info in reached.items():
                row = await entities_crud.get_entity(db, eid)
                names[row["name"] if row else eid] = (
                    f"depth={info['depth']} via={info['via_link_type']}"
                )
            print(f"spine check — reachable from OMI: {names}")
            mention_rows = await entities_crud.memories_mentioning(db, list(reached))
            print(f"mentions on reached entities: {mention_rows}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
