#!/usr/bin/env python3
"""One-time backfill: embed existing observations into Qdrant episodic_memory.

Reads all observations from SQLite that don't already have a corresponding
Qdrant entry (identified by obs:<id> tag), embeds them via MemoryStore,
and upserts to Qdrant.

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/backfill_observation_embeddings.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")


async def main(dry_run: bool = False) -> None:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.env import genesis_db_path, qdrant_url
    from genesis.memory.embeddings import EmbeddingProvider
    from genesis.memory.linker import MemoryLinker
    from genesis.memory.store import MemoryStore

    db = await aiosqlite.connect(str(genesis_db_path()))
    db.row_factory = aiosqlite.Row

    qdrant = QdrantClient(url=qdrant_url(), timeout=10)
    embedding = EmbeddingProvider()
    linker = MemoryLinker(qdrant_client=qdrant, db=db)
    store = MemoryStore(
        embedding_provider=embedding,
        qdrant_client=qdrant,
        db=db,
        linker=linker,
    )

    # Get all observations
    cursor = await db.execute(
        "SELECT id, source, type, content, created_at FROM observations ORDER BY created_at ASC",
    )
    all_obs = [dict(row) for row in await cursor.fetchall()]
    logger.info("Found %d total observations", len(all_obs))

    # Get existing Qdrant obs tags to skip already-embedded
    existing_obs_ids: set[str] = set()
    offset = None
    while True:
        result = qdrant.scroll(
            collection_name="episodic_memory",
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, next_offset = result
        for p in points:
            tags = p.payload.get("tags", [])
            for t in tags:
                if t.startswith("obs:"):
                    existing_obs_ids.add(t[4:])
        if next_offset is None:
            break
        offset = next_offset

    logger.info("Found %d observations already in Qdrant", len(existing_obs_ids))

    # Filter to unembedded observations
    to_embed = [o for o in all_obs if o["id"] not in existing_obs_ids]
    logger.info("Need to embed %d observations", len(to_embed))

    if dry_run:
        logger.info("DRY RUN — would embed %d observations. Exiting.", len(to_embed))
        await db.close()
        return

    succeeded = 0
    failed = 0
    for i, obs in enumerate(to_embed):
        content = obs["content"][:2000]
        try:
            await store.store(
                content,
                obs["source"],
                memory_type="episodic",
                tags=[obs["type"], f"obs:{obs['id']}"],
            )
            succeeded += 1
            if (i + 1) % 20 == 0:
                logger.info("Progress: %d/%d embedded", i + 1, len(to_embed))
        except Exception:
            failed += 1
            logger.warning("Failed to embed observation %s", obs["id"], exc_info=True)

    logger.info(
        "Backfill complete: %d succeeded, %d failed out of %d",
        succeeded, failed, len(to_embed),
    )

    await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Don't actually embed")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
