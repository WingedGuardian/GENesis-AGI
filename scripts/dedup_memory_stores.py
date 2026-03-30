#!/usr/bin/env python3
"""One-time memory dedup cleanup — removes duplicate FTS entries and Qdrant points.

The memory_fts table has accumulated duplicates (identical content stored
multiple times with different memory_ids). Each duplicate also has a Qdrant
point and potentially memory_links and pending_embeddings entries.

CRITICAL: The FTS ``collection`` column is unreliable (uniformly
``episodic_memory`` regardless of actual Qdrant placement). This script
tries BOTH Qdrant collections for every deletion.

Usage:
    python scripts/dedup_memory_stores.py --dry-run    # Report only (default)
    python scripts/dedup_memory_stores.py --execute    # Actually delete

Safety:
    - --dry-run by default (must pass --execute explicitly)
    - Auto-backs up DB before execution
    - Creates Qdrant snapshot before execution
    - Wraps all SQLite changes in explicit transaction
    - Only deletes from FTS IDs that successfully deleted from Qdrant
    - Cleans pending_embeddings to prevent duplicate regeneration
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.http.models import PointIdsList

# Ensure genesis package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from genesis.env import genesis_db_path, qdrant_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = str(genesis_db_path())
QDRANT_URL = qdrant_url()
QDRANT_COLLECTIONS = ["episodic_memory", "knowledge_base"]
BATCH_SIZE = 100


def find_duplicates(
    db: sqlite3.Connection,
) -> dict[int, tuple[int, str, list[tuple[int, str]]]]:
    """Find duplicate content groups.

    Returns {group_index: (survivor_rowid, survivor_memory_id,
             [(dup_rowid, dup_memory_id), ...])} for groups with duplicates.
    """
    cursor = db.execute(
        "SELECT rowid, memory_id, content FROM memory_fts ORDER BY rowid ASC"
    )

    # Group by exact content — key is content string
    content_groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for rowid, memory_id, content in cursor:
        content_groups[content].append((rowid, memory_id))

    # Build result: only groups with duplicates, survivor is lowest rowid
    result = {}
    for idx, (_content, entries) in enumerate(content_groups.items()):
        if len(entries) > 1:
            survivor_rowid, survivor_id = entries[0]
            duplicates = entries[1:]
            result[idx] = (survivor_rowid, survivor_id, duplicates)

    return result


def redirect_links(
    db: sqlite3.Connection,
    duplicate_id: str,
    survivor_id: str,
    *,
    dry_run: bool,
) -> int:
    """Redirect memory_links from duplicate to survivor. Returns redirect count.

    Handles PK collisions by deleting conflicting existing links before
    updating, since memory_links has PRIMARY KEY (source_id, target_id).
    """
    if dry_run:
        count = db.execute(
            "SELECT COUNT(*) FROM memory_links "
            "WHERE source_id = ? OR target_id = ?",
            (duplicate_id, duplicate_id),
        ).fetchone()[0]
        return count

    redirected = 0

    # Phase 1: Delete links that would collide after redirect.
    # If (survivor → X) already exists and (duplicate → X) needs redirect,
    # delete the duplicate's link (survivor's link is canonical).
    db.execute(
        "DELETE FROM memory_links "
        "WHERE source_id = ? AND target_id IN ("
        "  SELECT target_id FROM memory_links WHERE source_id = ?"
        ")",
        (duplicate_id, survivor_id),
    )
    db.execute(
        "DELETE FROM memory_links "
        "WHERE target_id = ? AND source_id IN ("
        "  SELECT source_id FROM memory_links WHERE target_id = ?"
        ")",
        (duplicate_id, survivor_id),
    )

    # Phase 2: Redirect remaining links (no collision risk now)
    cursor = db.execute(
        "UPDATE memory_links SET source_id = ? "
        "WHERE source_id = ?",
        (survivor_id, duplicate_id),
    )
    redirected += cursor.rowcount

    cursor = db.execute(
        "UPDATE memory_links SET target_id = ? "
        "WHERE target_id = ?",
        (survivor_id, duplicate_id),
    )
    redirected += cursor.rowcount

    # Phase 3: Clean up self-referential links (scoped to this survivor only)
    db.execute(
        "DELETE FROM memory_links WHERE source_id = target_id AND source_id = ?",
        (survivor_id,),
    )

    return redirected


def delete_qdrant_points(
    client: QdrantClient,
    point_ids: list[str],
    *,
    dry_run: bool,
) -> tuple[set[str], set[str]]:
    """Delete points from BOTH Qdrant collections.

    Returns (succeeded_ids, failed_ids). Tries both collections for each
    ID since FTS collection column is unreliable.
    """
    if dry_run:
        return set(point_ids), set()

    # Validate UUIDs upfront
    valid_ids: list[str] = []
    for pid in point_ids:
        try:
            valid_ids.append(str(UUID(pid)))
        except ValueError:
            logger.warning("Invalid UUID, skipping: %s", pid)

    # Track per-collection success. An ID is "succeeded" if at least one
    # collection's batch delete completed without error for that ID's batch.
    succeeded: set[str] = set()
    batch_failed: set[str] = set()  # IDs in batches that raised exceptions

    for collection in QDRANT_COLLECTIONS:
        for i in range(0, len(valid_ids), BATCH_SIZE):
            batch = valid_ids[i: i + BATCH_SIZE]
            try:
                client.delete(
                    collection_name=collection,
                    points_selector=PointIdsList(points=batch),
                )
                # Qdrant delete is idempotent — no error if point doesn't exist
                succeeded.update(batch)
            except Exception:
                logger.error(
                    "Failed to delete batch %d from %s",
                    i // BATCH_SIZE, collection, exc_info=True,
                )
                # Track which IDs were in the failed batch
                batch_failed.update(batch)

    # An ID is truly failed only if it was in a failed batch AND never succeeded
    actually_failed = batch_failed - succeeded
    # Add invalid UUIDs to failed set
    actually_failed.update(set(point_ids) - set(valid_ids))
    return succeeded, actually_failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deduplicate memory stores (FTS5 + Qdrant + links)",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually delete duplicates (default: dry-run report only)",
    )
    args = parser.parse_args()

    dry_run = not args.execute
    mode = "DRY RUN" if dry_run else "EXECUTE"
    logger.info("=== Memory Dedup %s ===", mode)

    # --- Safety: backup before execution ---
    if not dry_run:
        backup_path = Path(DB_PATH + ".pre-dedup")
        if not backup_path.exists():
            shutil.copy2(DB_PATH, backup_path)
            logger.info("Backed up DB to %s", backup_path)
        else:
            logger.info("Backup already exists at %s", backup_path)

    # Connect
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    client = QdrantClient(url=QDRANT_URL)

    # --- Safety: Qdrant snapshot before execution ---
    if not dry_run:
        try:
            snapshot = client.create_snapshot(collection_name="episodic_memory")
            logger.info("Qdrant episodic_memory snapshot: %s", snapshot)
            snapshot2 = client.create_snapshot(collection_name="knowledge_base")
            logger.info("Qdrant knowledge_base snapshot: %s", snapshot2)
        except Exception:
            logger.error("Failed to create Qdrant snapshots", exc_info=True)
            logger.error("Aborting — cannot proceed without snapshots")
            return 1

    # Get initial counts
    total_fts = db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    total_links = db.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
    total_pending = db.execute("SELECT COUNT(*) FROM pending_embeddings").fetchone()[0]
    logger.info(
        "FTS entries: %d | Memory links: %d | Pending embeddings: %d",
        total_fts, total_links, total_pending,
    )

    # Find duplicates
    dupe_groups = find_duplicates(db)
    total_dupes = sum(len(dupes) for _, _, dupes in dupe_groups.values())
    logger.info(
        "Found %d duplicate groups with %d total duplicate entries",
        len(dupe_groups), total_dupes,
    )

    if total_dupes == 0:
        logger.info("No duplicates found. Nothing to do.")
        return 0

    # Collect all duplicate memory_ids and their survivors
    all_duplicate_ids: list[str] = []
    survivor_map: dict[str, str] = {}  # duplicate_id → survivor_id
    fts_rowids_to_delete: list[int] = []

    for _, (_, survivor_id, duplicates) in dupe_groups.items():
        for dup_rowid, dup_id in duplicates:
            all_duplicate_ids.append(dup_id)
            survivor_map[dup_id] = survivor_id
            fts_rowids_to_delete.append(dup_rowid)

    logger.info("  Duplicate memory_ids to process: %d", len(all_duplicate_ids))
    logger.info("  FTS rows to delete: %d", len(fts_rowids_to_delete))

    # --- Phase 1 (dry-run): Report counts ---
    if dry_run:
        links_affected = 0
        for dup_id, surv_id in survivor_map.items():
            links_affected += redirect_links(db, dup_id, surv_id, dry_run=True)
        logger.info("  Memory links to redirect: %d", links_affected)

        pending_affected = 0
        for dup_id in all_duplicate_ids:
            row = db.execute(
                "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = ?",
                (dup_id,),
            ).fetchone()
            pending_affected += row[0]
        logger.info("  Pending embeddings to clean: %d", pending_affected)
        logger.info("=== DRY RUN complete. Use --execute to apply. ===")
        return 0

    # --- Phase 2: Delete from Qdrant (BOTH collections) ---
    qdrant_ok, qdrant_failed = delete_qdrant_points(
        client, all_duplicate_ids, dry_run=False,
    )
    logger.info(
        "Qdrant: %d succeeded, %d failed", len(qdrant_ok), len(qdrant_failed),
    )

    if qdrant_failed:
        logger.warning(
            "Skipping SQLite cleanup for %d IDs that failed Qdrant deletion",
            len(qdrant_failed),
        )

    # --- Phase 3: ALL SQLite changes in one explicit transaction ---
    # Only process IDs whose Qdrant points were successfully deleted
    safe_rowids = [
        rid for rid, did in zip(fts_rowids_to_delete, all_duplicate_ids, strict=True)
        if did not in qdrant_failed
    ]
    safe_ids = [did for did in all_duplicate_ids if did not in qdrant_failed]
    safe_survivor_map = {
        did: sid for did, sid in survivor_map.items() if did not in qdrant_failed
    }

    db.execute("BEGIN")
    try:
        # Redirect memory_links (inside transaction)
        links_redirected = 0
        for dup_id, surv_id in safe_survivor_map.items():
            links_redirected += redirect_links(
                db, dup_id, surv_id, dry_run=False,
            )
        logger.info("Redirected %d memory links", links_redirected)

        # Delete FTS entries
        for i in range(0, len(safe_rowids), BATCH_SIZE):
            batch = safe_rowids[i: i + BATCH_SIZE]
            placeholders = ",".join("?" * len(batch))
            db.execute(
                f"DELETE FROM memory_fts WHERE rowid IN ({placeholders})",  # noqa: S608
                batch,
            )
        logger.info("Deleted %d FTS rows", len(safe_rowids))

        # Clean pending_embeddings for deleted IDs
        pending_cleaned = 0
        for dup_id in safe_ids:
            cursor = db.execute(
                "DELETE FROM pending_embeddings WHERE memory_id = ?",
                (dup_id,),
            )
            pending_cleaned += cursor.rowcount
        logger.info("Cleaned %d pending_embeddings entries", pending_cleaned)

        db.commit()
    except Exception:
        db.rollback()
        logger.error("SQLite transaction failed — rolled back", exc_info=True)
        return 1

    # --- Phase 4: Verify ---
    final_fts = db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    final_links = db.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
    final_pending = db.execute("SELECT COUNT(*) FROM pending_embeddings").fetchone()[0]
    logger.info("Final FTS: %d (was %d)", final_fts, total_fts)
    logger.info("Final links: %d (was %d)", final_links, total_links)
    logger.info("Final pending: %d (was %d)", final_pending, total_pending)

    try:
        episodic = client.get_collection("episodic_memory").points_count
        knowledge = client.get_collection("knowledge_base").points_count
        qdrant_total = episodic + knowledge
        logger.info(
            "Qdrant: episodic=%d + knowledge=%d = %d (FTS=%d)",
            episodic, knowledge, qdrant_total, final_fts,
        )
        if qdrant_total != final_fts:
            logger.warning(
                "PARITY MISMATCH: Qdrant total (%d) != FTS (%d)",
                qdrant_total, final_fts,
            )
        else:
            logger.info("Parity check PASSED")
    except Exception:
        logger.error("Could not verify Qdrant parity", exc_info=True)

    db.close()
    logger.info("=== Dedup COMPLETE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
