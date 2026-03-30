#!/usr/bin/env python3
"""One-time migration: move pipeline data from episodic_memory to knowledge_base.

Pipeline data (crypto-ops, prediction-markets web scrapes) was stored in
episodic_memory alongside cognitive observations. This polluted retrieval —
96% of points were pipeline data, drowning out observations.

This script:
1. Scrolls episodic_memory for points WITHOUT obs: tags (pipeline data)
2. Inserts them into knowledge_base with same vectors + payloads
3. Deletes originals from episodic_memory
4. Also backfills retrieved_count from SQLite → Qdrant for observations

Safe to run multiple times — skips already-migrated points.
"""

import argparse
import logging
import sys

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def migrate_pipeline_data(client: QdrantClient, *, dry_run: bool = False) -> dict:
    """Move pipeline points from episodic_memory to knowledge_base."""
    stats = {"scanned": 0, "migrated": 0, "skipped_obs": 0, "errors": 0}
    batch_size = 100
    offset = None

    # Ensure knowledge_base collection exists with same config as episodic_memory
    collections = {c.name for c in client.get_collections().collections}
    if "knowledge_base" not in collections:
        log.error("knowledge_base collection does not exist — create it first")
        return stats

    to_migrate: list[PointStruct] = []
    to_delete: list[str] = []

    while True:
        points, next_offset = client.scroll(
            "episodic_memory",
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break

        for p in points:
            stats["scanned"] += 1
            tags = p.payload.get("tags", []) if p.payload else []
            has_obs_tag = any(str(t).startswith("obs:") for t in tags)

            if has_obs_tag:
                stats["skipped_obs"] += 1
                continue

            to_migrate.append(
                PointStruct(id=p.id, vector=p.vector, payload=p.payload)
            )
            to_delete.append(p.id)

        offset = next_offset
        if offset is None:
            break

    log.info(
        "Scan complete: %d total, %d pipeline (to migrate), %d observations (keep)",
        stats["scanned"], len(to_migrate), stats["skipped_obs"],
    )

    if dry_run:
        log.info("DRY RUN — no changes made")
        stats["migrated"] = len(to_migrate)
        return stats

    # Insert into knowledge_base in batches
    insert_batch = 50
    for i in range(0, len(to_migrate), insert_batch):
        batch = to_migrate[i : i + insert_batch]
        try:
            client.upsert("knowledge_base", points=batch)
            stats["migrated"] += len(batch)
            log.info("Inserted batch %d-%d into knowledge_base", i, i + len(batch))
        except Exception:
            log.exception("Failed to insert batch %d-%d", i, i + len(batch))
            stats["errors"] += len(batch)

    # Delete from episodic_memory in batches
    for i in range(0, len(to_delete), insert_batch):
        batch = to_delete[i : i + insert_batch]
        try:
            client.delete("episodic_memory", points_selector=batch)
            log.info("Deleted batch %d-%d from episodic_memory", i, i + len(batch))
        except Exception:
            log.exception("Failed to delete batch %d-%d", i, i + len(batch))
            stats["errors"] += len(batch)

    return stats


def backfill_retrieved_count(client: QdrantClient, db_path: str, *, dry_run: bool = False) -> int:
    """Sync retrieved_count from SQLite → Qdrant for observations."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, retrieved_count FROM observations WHERE retrieved_count > 0"
    ).fetchall()
    conn.close()

    updated = 0
    for row in rows:
        obs_id = row["id"]
        count = row["retrieved_count"]
        if dry_run:
            updated += 1
            continue
        try:
            client.set_payload(
                "episodic_memory",
                payload={"retrieved_count": count},
                points=[obs_id],
            )
            updated += 1
        except Exception:
            log.warning("Failed to update retrieved_count for %s", obs_id)

    log.info("Backfilled retrieved_count for %d observations%s", updated, " (dry run)" if dry_run else "")
    return updated


def main() -> None:
    from urllib.parse import urlsplit

    from genesis.env import genesis_db_path, qdrant_url

    parsed_qdrant = urlsplit(qdrant_url())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without modifying data")
    parser.add_argument("--db", default=str(genesis_db_path()), help="Path to genesis.db")
    parser.add_argument("--qdrant-host", default=parsed_qdrant.hostname or "localhost", help="Qdrant host")
    parser.add_argument("--qdrant-port", type=int, default=parsed_qdrant.port or 6333, help="Qdrant port")
    args = parser.parse_args()

    client = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)

    log.info("=== Step 1: Migrate pipeline data to knowledge_base ===")
    stats = migrate_pipeline_data(client, dry_run=args.dry_run)
    log.info("Migration stats: %s", stats)

    log.info("=== Step 2: Backfill retrieved_count from SQLite ===")
    backfill_retrieved_count(client, args.db, dry_run=args.dry_run)

    if stats["errors"] > 0:
        log.error("Completed with %d errors", stats["errors"])
        sys.exit(1)

    log.info("Done.")


if __name__ == "__main__":
    main()
