#!/usr/bin/env python3
"""One-time cleanup: remove low-confidence pipeline noise from memory.

Deletes memories with confidence < 0.4 from knowledge_base collection,
both from SQLite (memory_metadata, memory_fts, memory_links) and Qdrant.

Also backfills NULL confidence on rule-classified memories with 0.8.

Run from genesis root with venv active:
    python scripts/cleanup_pipeline_noise.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure genesis is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def main(dry_run: bool = False) -> None:
    import aiosqlite

    # Always use the real DB, not the worktree copy
    db_path = Path.home() / "genesis" / "data" / "genesis.db"
    logger.info("Database: %s", db_path)

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        # --- Phase 1: Identify noise memories ---
        cursor = await db.execute(
            "SELECT memory_id FROM memory_metadata "
            "WHERE confidence IS NOT NULL AND confidence < 0.4 "
            "AND collection = 'knowledge_base'"
        )
        rows = await cursor.fetchall()
        noise_ids = [r[0] for r in rows]
        logger.info("Found %d low-confidence knowledge_base memories to delete", len(noise_ids))

        if not noise_ids:
            logger.info("Nothing to clean up")
        elif dry_run:
            logger.info("[DRY RUN] Would delete %d memories", len(noise_ids))
            for mid in noise_ids[:5]:
                logger.info("  Sample: %s", mid)
            if len(noise_ids) > 5:
                logger.info("  ... and %d more", len(noise_ids) - 5)
        else:
            # Delete from memory_links (cascade)
            link_count = 0
            for mid in noise_ids:
                cursor = await db.execute(
                    "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
                    (mid, mid),
                )
                link_count += cursor.rowcount

            # Delete from memory_fts (FTS5 table with memory_id column)
            for mid in noise_ids:
                await db.execute(
                    "DELETE FROM memory_fts WHERE memory_id = ?",
                    (mid,),
                )

            # Delete from memory_metadata
            placeholders = ",".join("?" * len(noise_ids))
            cursor = await db.execute(
                f"DELETE FROM memory_metadata WHERE memory_id IN ({placeholders})",
                noise_ids,
            )
            meta_deleted = cursor.rowcount

            await db.commit()
            logger.info(
                "SQLite cleanup: %d metadata rows, %d link rows deleted",
                meta_deleted, link_count,
            )

            # Delete from Qdrant
            try:
                from qdrant_client import QdrantClient
                from qdrant_client.models import PointIdsList

                client = QdrantClient(url="http://localhost:6333")
                # Qdrant accepts UUID strings as point IDs
                batch_size = 100
                for i in range(0, len(noise_ids), batch_size):
                    batch = noise_ids[i : i + batch_size]
                    client.delete(
                        collection_name="knowledge_base",
                        points_selector=PointIdsList(points=batch),
                    )
                    logger.info("Qdrant: deleted batch %d-%d", i, i + len(batch))
            except Exception:
                logger.exception("Qdrant cleanup failed — SQLite already cleaned")

        # --- Phase 2: Backfill NULL confidence on rules ---
        cursor = await db.execute(
            "SELECT COUNT(*) FROM memory_metadata "
            "WHERE memory_class = 'rule' AND confidence IS NULL"
        )
        null_rules = (await cursor.fetchone())[0]

        if null_rules == 0:
            logger.info("No null-confidence rules to backfill")
        elif dry_run:
            logger.info("[DRY RUN] Would backfill %d rule memories with confidence=0.8", null_rules)
        else:
            await db.execute(
                "UPDATE memory_metadata SET confidence = 0.8 "
                "WHERE memory_class = 'rule' AND confidence IS NULL"
            )
            await db.commit()
            logger.info("Backfilled %d rule memories with confidence=0.8", null_rules)

        # --- Summary ---
        cursor = await db.execute(
            "SELECT collection, COUNT(*) FROM memory_metadata GROUP BY collection"
        )
        for row in await cursor.fetchall():
            logger.info("  %s: %d memories", row[0], row[1])

        cursor = await db.execute(
            "SELECT COUNT(*) FROM memory_metadata WHERE confidence IS NULL"
        )
        null_count = (await cursor.fetchone())[0]
        logger.info("  Remaining NULL confidence: %d", null_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without acting")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
