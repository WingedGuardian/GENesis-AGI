#!/usr/bin/env python3
"""Entity-layer backfill (approved HYBRID strategy, 2026-07-09).

Two passes, both mechanical — NO LLM calls:

1. **Anchor pass** — regex code-anchor extraction over every
   ``memory_fts`` row (paths, genesis.* symbols, PR#s, SHAs) →
   ``entity_mentions`` with ``source='backfill_mechanical'``.
2. **Seed-FTS pass** — for each seed entity, FTS5 MATCH on its name →
   ``entity_mentions`` with provenance ``INFERRED``, confidence 0.6,
   ``source='backfill_fts'``. This is what makes the repo-split memory
   reachable ORGANICALLY (its content names GENesis-Voice / voice edge),
   not only via the hand-seeded spine.

Forward-only for LLM-extracted named entities (no 49.7K re-extraction):
cost, near-duplicate enrichment waste, and it would distort the
engine-re-evaluation load data (follow-up 3ae26455).

Default is DRY-RUN (counts only). Pass --apply to write.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

BATCH = 2000


async def anchor_pass(db, apply: bool) -> dict:
    from genesis.memory.entity_anchors import extract_anchors, record_anchors

    scanned = mentions = memories_with = 0
    last_rowid = 0
    while True:
        rows = await db.execute_fetchall(
            "SELECT rowid, memory_id, content FROM memory_fts "
            "WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last_rowid, BATCH),
        )
        if not rows:
            break
        for rowid, memory_id, content in rows:
            last_rowid = rowid
            scanned += 1
            if not content or not memory_id:
                continue
            anchors = extract_anchors(content)
            if not anchors:
                continue
            memories_with += 1
            if apply:
                mentions += await record_anchors(
                    db, memory_id, content, source="backfill_mechanical",
                )
            else:
                mentions += len(anchors)
        print(f"  anchor pass: {scanned} scanned, {mentions} mentions", end="\r")
    print()
    return {
        "scanned": scanned,
        "memories_with_anchors": memories_with,
        "anchor_mentions": mentions,
    }


async def seed_fts_pass(db, apply: bool) -> dict:
    from genesis.db.crud import entities as entities_crud
    from genesis.memory.entity_registry import norm
    from genesis.memory.entity_seed import SEED_ENTITIES

    total = 0
    per_entity: dict[str, int] = {}
    for name, _entity_type, _summary in SEED_ENTITIES:
        entity = await entities_crud.get_by_norm_name(db, norm_name=norm(name))
        if entity is None:
            print(f"  seed entity missing (run apply_entity_seed first): {name}")
            continue
        # Quoted phrase match; FTS5 default tokenizer folds case and
        # splits hyphens, so "voice-edge-device" matches "voice edge".
        query = '"' + name.replace('"', "") + '"'
        try:
            rows = await db.execute_fetchall(
                "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? "
                "LIMIT 500",
                (query,),
            )
        except Exception as exc:
            print(f"  FTS query failed for {name!r}: {exc}")
            continue
        count = 0
        for (memory_id,) in rows:
            if not memory_id:
                continue
            if apply:
                await entities_crud.upsert_mention(
                    db, memory_id=memory_id, entity_id=entity["entity_id"],
                    provenance="INFERRED", confidence=0.6,
                    source="backfill_fts", _commit=False,
                )
            count += 1
        if apply:
            await db.commit()
        per_entity[name] = count
        total += count
    return {"seed_mentions": total, "per_entity": per_entity}


async def main() -> None:
    import aiosqlite

    from genesis import env as genesis_env

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    db_path = args.db or str(genesis_env.genesis_db_path())

    db = await aiosqlite.connect(db_path)
    try:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] entity backfill against {db_path}")
        anchor = await anchor_pass(db, args.apply)
        print(f"anchor pass: {anchor}")
        seed = await seed_fts_pass(db, args.apply)
        print(f"seed-FTS pass: {seed['seed_mentions']} mentions")
        for name, count in sorted(seed["per_entity"].items(), key=lambda kv: -kv[1]):
            print(f"  {name}: {count}")
        target = "9d36f039-3126-4721-8c71-027df1a94e2a"
        rows = await db.execute_fetchall(
            "SELECT entity_id, provenance, source FROM entity_mentions "
            "WHERE memory_id = ?",
            (target,),
        ) if args.apply else []
        if args.apply:
            print(f"repo-split memory ({target[:8]}) mentions: {list(rows)}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
