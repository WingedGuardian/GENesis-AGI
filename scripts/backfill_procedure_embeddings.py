#!/usr/bin/env python3
"""One-time backfill: embed procedural_memory principles that are missing embeddings.

The proactive procedure hook (PR #321) uses cosine similarity on
principle_embedding to surface relevant procedures. Rows with NULL embedding
are invisible to that hook.

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/backfill_procedure_embeddings.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_procedure_embeddings")


async def main(dry_run: bool = False) -> None:
    import aiosqlite

    from genesis.env import genesis_db_path
    from genesis.learning.procedural.embedding import pack_embedding
    from genesis.memory.embeddings import EmbeddingProvider

    db_path = genesis_db_path()
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row

    embedding = EmbeddingProvider()

    # Select all procedures with NULL principle_embedding
    cursor = await db.execute(
        "SELECT id, task_type, principle FROM procedural_memory "
        "WHERE principle_embedding IS NULL "
        "ORDER BY created_at ASC",
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    logger.info("Found %d procedures with missing embeddings", len(rows))

    if not rows:
        logger.info("Nothing to backfill. Exiting.")
        await db.close()
        return

    if dry_run:
        logger.info("DRY RUN — would embed %d procedures:", len(rows))
        for row in rows:
            logger.info(
                "  [%s] %s: %s",
                row["id"][:8], row["task_type"], row["principle"][:80],
            )
        await db.close()
        return

    succeeded = 0
    failed = 0
    for i, row in enumerate(rows):
        proc_id = row["id"]
        principle = row["principle"]
        try:
            vector = await embedding.embed(principle)
            blob = pack_embedding(vector)
            await db.execute(
                "UPDATE procedural_memory SET principle_embedding = ? WHERE id = ?",
                (blob, proc_id),
            )
            await db.commit()
            succeeded += 1
            logger.info(
                "  [%d/%d] Embedded %s (%s)",
                i + 1, len(rows), proc_id[:8], row["task_type"],
            )
        except Exception:
            failed += 1
            logger.warning(
                "  [%d/%d] FAILED %s (%s)",
                i + 1, len(rows), proc_id[:8], row["task_type"],
                exc_info=True,
            )

    logger.info(
        "Backfill complete: %d succeeded, %d failed out of %d",
        succeeded, failed, len(rows),
    )
    await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill missing principle_embedding in procedural_memory",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
