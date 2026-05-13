#!/usr/bin/env python3
"""One-time migration: move curated KB entries from episodic_memory → knowledge_base.

Root cause: commit e636eaf6 (2026-03-24) ran migrate_knowledge_to_episodic.py
which moved ALL knowledge_base entries to episodic_memory without filtering.
This included 408 curated entries (cloud architecture, AWS security, etc.)
that belonged in knowledge_base.

Usage:
    python scripts/migrate_curated_to_knowledge.py --dry-run   # preview only
    python scripts/migrate_curated_to_knowledge.py --execute   # actually migrate

Safety:
    - Dry-run by default (requires explicit --execute)
    - Idempotent: upsert into knowledge_base, skip if already present
    - Vectors are copied with payloads (same 1024-dim cosine config)
    - SQLite updates in a single transaction
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import requests

_QDRANT_URL = "http://localhost:6333"
_DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"
_BATCH_SIZE = 100


def _scroll_curated(collection: str) -> list[dict]:
    """Scroll all curated points from a Qdrant collection."""
    points: list[dict] = []
    offset = None
    while True:
        body: dict = {
            "filter": {"must": [{"key": "source_pipeline", "match": {"value": "curated"}}]},
            "limit": _BATCH_SIZE,
            "with_payload": True,
            "with_vector": True,
        }
        if offset is not None:
            body["offset"] = offset
        resp = requests.post(
            f"{_QDRANT_URL}/collections/{collection}/points/scroll",
            json=body, timeout=30,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        batch = result.get("points", [])
        points.extend(batch)
        offset = result.get("next_page_offset")
        if not offset or not batch:
            break
    return points


def _upsert_batch(collection: str, points: list[dict]) -> None:
    """Upsert a batch of points into a Qdrant collection."""
    formatted = []
    for p in points:
        formatted.append({
            "id": p["id"],
            "vector": p["vector"],
            "payload": p.get("payload", {}),
        })
    resp = requests.put(
        f"{_QDRANT_URL}/collections/{collection}/points",
        json={"points": formatted},
        timeout=60,
    )
    resp.raise_for_status()


def _delete_points(collection: str, ids: list[str]) -> None:
    """Delete points by ID from a Qdrant collection."""
    resp = requests.post(
        f"{_QDRANT_URL}/collections/{collection}/points/delete",
        json={"points": ids},
        timeout=30,
    )
    resp.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only")
    group.add_argument("--execute", action="store_true", help="Actually migrate")
    args = parser.parse_args()

    # Step 1: Find curated entries in episodic_memory
    print("Scrolling episodic_memory for source_pipeline='curated'...")
    epi_points = _scroll_curated("episodic_memory")
    print(f"  Found {len(epi_points)} curated entries in episodic_memory")

    if not epi_points:
        print("Nothing to migrate.")
        return

    # Step 2: Check which IDs already exist in knowledge_base
    kb_points = _scroll_curated("knowledge_base")
    kb_ids = {str(p["id"]) for p in kb_points}
    print(f"  Found {len(kb_ids)} curated entries already in knowledge_base")

    to_move = [p for p in epi_points if str(p["id"]) not in kb_ids]
    to_skip = [p for p in epi_points if str(p["id"]) in kb_ids]
    print(f"  To move: {len(to_move)}, already in KB (skip upsert, still delete from episodic): {len(to_skip)}")

    all_ids = [str(p["id"]) for p in epi_points]

    if args.dry_run:
        print("\n[DRY RUN] Would migrate these entries:")
        for p in to_move[:10]:
            payload = p.get("payload", {})
            print(f"  {p['id']}: {payload.get('source', '?')[:60]}")
        if len(to_move) > 10:
            print(f"  ... and {len(to_move) - 10} more")
        print(f"\n[DRY RUN] Would delete {len(all_ids)} entries from episodic_memory")
        print(f"[DRY RUN] Would update {len(all_ids)} rows in memory_metadata + memory_fts")
        return

    # Step 3: Upsert into knowledge_base (batched)
    print(f"\nUpserting {len(to_move)} points into knowledge_base...")
    for i in range(0, len(to_move), _BATCH_SIZE):
        batch = to_move[i : i + _BATCH_SIZE]
        _upsert_batch("knowledge_base", batch)
        print(f"  Upserted batch {i // _BATCH_SIZE + 1} ({len(batch)} points)")

    # Step 4: Delete ALL curated entries from episodic_memory
    print(f"Deleting {len(all_ids)} points from episodic_memory...")
    for i in range(0, len(all_ids), _BATCH_SIZE):
        batch = all_ids[i : i + _BATCH_SIZE]
        _delete_points("episodic_memory", batch)
        print(f"  Deleted batch {i // _BATCH_SIZE + 1} ({len(batch)} points)")

    # Step 5: Update SQLite
    print(f"Updating SQLite ({len(all_ids)} rows)...")
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        cursor = conn.cursor()
        for mid in all_ids:
            cursor.execute(
                "UPDATE memory_metadata SET collection = 'knowledge_base' "
                "WHERE memory_id = ? AND collection = 'episodic_memory'",
                (mid,),
            )
            cursor.execute(
                "UPDATE memory_fts SET collection = 'knowledge_base' "
                "WHERE memory_id = ? AND collection = 'episodic_memory'",
                (mid,),
            )
        conn.commit()
        print(f"  SQLite updated ({cursor.rowcount} rows affected in last statement)")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Step 6: Verify
    print("\nVerification:")
    epi_after = _scroll_curated("episodic_memory")
    kb_after = _scroll_curated("knowledge_base")
    print(f"  episodic_memory curated: {len(epi_after)} (was {len(epi_points)})")
    print(f"  knowledge_base curated: {len(kb_after)} (was {len(kb_ids)})")

    if len(epi_after) == 0:
        print("\nMigration complete. All curated entries moved to knowledge_base.")
    else:
        print(f"\nWARNING: {len(epi_after)} curated entries still in episodic_memory!")
        sys.exit(1)


if __name__ == "__main__":
    main()
