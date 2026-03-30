#!/usr/bin/env python3
"""One-time migration: move internal data from knowledge_base to episodic_memory.

The knowledge_base Qdrant collection was intended for external domain knowledge
but accumulated 539 internal entries due to a routing bug (_COLLECTION_MAP sent
memory_type="knowledge" to knowledge_base). This script:

1. Scrolls all points from knowledge_base
2. Upserts them into episodic_memory with scope:"internal" tag added
3. Updates memory_fts collection values to match
4. Deduplicates by content hash (removes lower-confidence duplicates)
5. Optionally clears knowledge_base (--clear flag)

Safe to re-run (upsert is idempotent). Run with --dry-run first.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

import httpx

# Ensure genesis package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from genesis.env import genesis_db_path, qdrant_url

_QDRANT_URL = qdrant_url()
_DB_PATH = genesis_db_path()
_SRC = "knowledge_base"
_DST = "episodic_memory"
_BATCH_SIZE = 50


def _get_count(client: httpx.Client, collection: str) -> int:
    resp = client.get(f"{_QDRANT_URL}/collections/{collection}")
    resp.raise_for_status()
    return resp.json()["result"]["points_count"]


def _scroll_all(client: httpx.Client, collection: str) -> list[dict]:
    """Scroll all points from a collection."""
    points: list[dict] = []
    offset = None
    while True:
        body: dict = {"limit": _BATCH_SIZE, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        resp = client.post(
            f"{_QDRANT_URL}/collections/{collection}/points/scroll",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()["result"]
        batch = data.get("points", [])
        if not batch:
            break
        points.extend(batch)
        offset = data.get("next_page_offset")
        if offset is None:
            break
    return points


def _upsert_batch(client: httpx.Client, collection: str, points: list[dict]) -> None:
    """Upsert a batch of points into a collection."""
    qdrant_points = []
    for p in points:
        qdrant_points.append({
            "id": p["id"],
            "vector": p["vector"],
            "payload": p["payload"],
        })
    resp = client.put(
        f"{_QDRANT_URL}/collections/{collection}/points",
        json={"points": qdrant_points},
    )
    resp.raise_for_status()


def _add_scope_tag(points: list[dict]) -> list[dict]:
    """Add scope:'internal' to all points (these are misrouted internal entries)."""
    for p in points:
        p["payload"]["scope"] = "internal"
    return points


def _dedup_by_content(client: httpx.Client, collection: str) -> int:
    """Find and remove content-hash duplicates, keeping the higher-confidence copy."""
    all_points = _scroll_all(client, collection)
    content_map: dict[str, list[dict]] = {}

    for p in all_points:
        content = p.get("payload", {}).get("content", "")
        h = hashlib.sha256(content.encode()).hexdigest()
        content_map.setdefault(h, []).append(p)

    removed = 0
    for _h, dupes in content_map.items():
        if len(dupes) <= 1:
            continue
        # Keep highest confidence, remove rest
        dupes.sort(key=lambda x: x.get("payload", {}).get("confidence", 0), reverse=True)
        to_remove = [p["id"] for p in dupes[1:]]
        if to_remove:
            resp = client.post(
                f"{_QDRANT_URL}/collections/{collection}/points/delete",
                json={"points": to_remove},
            )
            resp.raise_for_status()
            removed += len(to_remove)

    return removed


def _update_fts(db_path: Path, dry_run: bool) -> int:
    """Update memory_fts collection column from knowledge_base to episodic_memory."""
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE collection = ?", (_SRC,)
        )
        count = cursor.fetchone()[0]

        if not dry_run and count > 0:
            conn.execute(
                "UPDATE memory_fts SET collection = ? WHERE collection = ?",
                (_DST, _SRC),
            )
            conn.commit()

        return count
    finally:
        conn.close()


def _clear_collection(client: httpx.Client, collection: str) -> None:
    """Delete all points from a collection."""
    # Get all point IDs
    points = _scroll_all(client, collection)
    if not points:
        return
    ids = [p["id"] for p in points]
    for i in range(0, len(ids), _BATCH_SIZE):
        batch = ids[i:i + _BATCH_SIZE]
        resp = client.post(
            f"{_QDRANT_URL}/collections/{collection}/points/delete",
            json={"points": batch},
        )
        resp.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate knowledge_base → episodic_memory")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without migrating")
    parser.add_argument("--clear", action="store_true", help="Clear knowledge_base after migration")
    parser.add_argument("--skip-dedup", action="store_true", help="Skip deduplication step")
    args = parser.parse_args()

    client = httpx.Client(timeout=30.0)

    # Pre-migration counts
    src_count = _get_count(client, _SRC)
    dst_count = _get_count(client, _DST)
    fts_count = _update_fts(_DB_PATH, dry_run=True)

    print("=== Pre-migration state ===")
    print(f"  {_SRC}: {src_count} points")
    print(f"  {_DST}: {dst_count} points")
    print(f"  memory_fts with collection='{_SRC}': {fts_count} rows")

    if args.dry_run:
        print("\n=== Dry run — no changes made ===")
        print(f"  Would migrate {src_count} points from {_SRC} → {_DST}")
        print(f"  Would update {fts_count} FTS5 rows")
        if args.clear:
            print(f"  Would clear {_SRC} after migration")
        return

    if src_count == 0:
        print("Nothing to migrate — knowledge_base is already empty.")
        return

    # Step 1: Scroll all from knowledge_base
    print(f"\n=== Step 1: Scrolling {src_count} points from {_SRC} ===")
    points = _scroll_all(client, _SRC)
    print(f"  Scrolled {len(points)} points")

    # Step 2: Add scope tag
    print("=== Step 2: Adding scope:'internal' tag ===")
    points = _add_scope_tag(points)

    # Step 3: Upsert into episodic_memory
    print(f"=== Step 3: Upserting into {_DST} ===")
    for i in range(0, len(points), _BATCH_SIZE):
        batch = points[i:i + _BATCH_SIZE]
        _upsert_batch(client, _DST, batch)
        print(f"  Upserted batch {i}-{i + len(batch)}")

    # Step 4: Update FTS5
    print("=== Step 4: Updating memory_fts collection values ===")
    updated = _update_fts(_DB_PATH, dry_run=False)
    print(f"  Updated {updated} FTS5 rows")

    # Step 5: Dedup
    if not args.skip_dedup:
        print(f"=== Step 5: Deduplicating {_DST} by content hash ===")
        removed = _dedup_by_content(client, _DST)
        print(f"  Removed {removed} duplicate points")
    else:
        print("=== Step 5: Skipped (--skip-dedup) ===")

    # Step 6: Verify
    new_dst_count = _get_count(client, _DST)
    print("\n=== Post-migration state ===")
    print(f"  {_DST}: {new_dst_count} points (was {dst_count})")
    print(f"  Expected: ~{dst_count + src_count} minus dedup removals")

    # Step 7: Clear knowledge_base
    if args.clear:
        print(f"\n=== Step 7: Clearing {_SRC} ===")
        _clear_collection(client, _SRC)
        final_src = _get_count(client, _SRC)
        print(f"  {_SRC}: {final_src} points (should be 0)")
    else:
        print(f"\n  Run with --clear to empty {_SRC}")

    print("\n=== Migration complete ===")


if __name__ == "__main__":
    main()
