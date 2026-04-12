#!/usr/bin/env python3
"""Backfill wing/room taxonomy on existing memories.

Updates both Qdrant payloads and SQLite memory_metadata with wing/room
classifications derived from content and tags.

Safe to run multiple times (idempotent — overwrites existing wing/room values).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from genesis.memory.taxonomy import classify


def main() -> None:
    db_path = Path.home() / "genesis" / "data" / "genesis.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    # Fetch all memories with content and tags
    rows = db.execute(
        "SELECT f.memory_id, f.content, f.tags, f.source_type "
        "FROM memory_fts f"
    ).fetchall()

    print(f"Found {len(rows)} memories to classify")

    # Classify each memory
    wing_counts: dict[str, int] = {}
    updates: list[tuple[str, str, str]] = []

    for row in rows:
        memory_id = row["memory_id"]
        content = row["content"] or ""
        tags_str = row["tags"] or ""
        tags = [t.strip() for t in tags_str.split() if t.strip()]

        result = classify(content, tags=tags)
        updates.append((result.wing, result.room, memory_id))
        wing_counts[result.wing] = wing_counts.get(result.wing, 0) + 1

    # Update SQLite memory_metadata
    updated = 0
    for wing, room, memory_id in updates:
        cursor = db.execute(
            "UPDATE memory_metadata SET wing = ?, room = ? WHERE memory_id = ?",
            (wing, room, memory_id),
        )
        if cursor.rowcount > 0:
            updated += 1

    db.commit()
    print(f"\nSQLite memory_metadata updated: {updated}/{len(updates)}")

    # Update Qdrant payloads
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(host="localhost", port=6333)
        qdrant_updated = 0

        for wing, room, memory_id in updates:
            try:
                client.set_payload(
                    collection_name="episodic_memory",
                    payload={"wing": wing, "room": room},
                    points=[memory_id],
                )
                qdrant_updated += 1
            except Exception:
                # Point may not exist in Qdrant (FTS5-only memories)
                pass

        print(f"Qdrant payloads updated: {qdrant_updated}/{len(updates)}")
    except Exception as e:
        print(f"Qdrant update skipped: {e}")

    # Update FTS5 tags to include wing: prefix
    fts_updated = 0
    for wing, _room, memory_id in updates:
        wing_tag = f"wing:{wing}"
        # Read current tags
        row = db.execute(
            "SELECT tags FROM memory_fts WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row:
            current_tags = row["tags"] or ""
            if wing_tag not in current_tags:
                new_tags = f"{current_tags} {wing_tag}".strip()
                # FTS5 update requires delete + reinsert of the row
                content_row = db.execute(
                    "SELECT content, source_type, collection FROM memory_fts WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                if content_row:
                    db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
                    db.execute(
                        "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (memory_id, content_row["content"], content_row["source_type"],
                         new_tags, content_row["collection"]),
                    )
                    fts_updated += 1

    db.commit()
    print(f"FTS5 tags updated with wing: {fts_updated}")

    # Summary
    print("\n=== Wing Distribution ===")
    for wing, count in sorted(wing_counts.items(), key=lambda x: -x[1]):
        print(f"  {wing}: {count}")

    db.close()


if __name__ == "__main__":
    main()
