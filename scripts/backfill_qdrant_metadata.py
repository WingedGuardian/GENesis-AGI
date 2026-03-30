#!/usr/bin/env python3
"""Backfill missing metadata on Qdrant episodic_memory points.

The reindex_fts_to_qdrant.py script stored minimal payloads (content,
source_type, memory_type, origin) — stripping source, created_at, tags,
confidence, scope. This script repairs those points via Qdrant's set_payload
API which MERGES fields without touching vectors or existing fields.

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/backfill_qdrant_metadata.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import httpx

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from genesis.env import genesis_db_path, qdrant_url  # noqa: E402

QDRANT_URL = qdrant_url()
COLLECTION = "episodic_memory"
# First cc_session — honest default for memories with lost timestamps
DEFAULT_CREATED_AT = "2026-03-10T00:00:00+00:00"


def _genesis_db_path() -> Path:
    return genesis_db_path()


def _scroll_all_points(client: httpx.Client) -> list[dict]:
    """Scroll through all Qdrant points, returning id + payload."""
    points: list[dict] = []
    offset = None
    while True:
        body: dict = {"limit": 100, "with_payload": True}
        if offset:
            body["offset"] = offset
        resp = client.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json().get("result", {})
        batch = data.get("points", [])
        if not batch:
            break
        points.extend(batch)
        offset = data.get("next_page_offset")
        if not offset:
            break
    return points


def main(dry_run: bool = False) -> None:
    client = httpx.Client(timeout=10.0)
    db_path = _genesis_db_path()
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    # Load FTS5 tags (may be empty if migration just ran)
    fts_tags: dict[str, str] = {}
    try:
        for row in db.execute("SELECT memory_id, tags FROM memory_fts"):
            if row["tags"]:
                fts_tags[row["memory_id"]] = row["tags"]
    except Exception as exc:
        print(f"Warning: couldn't read FTS5 tags: {exc}")

    # Scroll all Qdrant points
    points = _scroll_all_points(client)
    print(f"Total Qdrant points: {len(points)}")

    # Separate into needs-backfill and well-formed
    needs_backfill: list[dict] = []
    well_formed: list[dict] = []
    for p in points:
        payload = p.get("payload", {})
        if not payload.get("source"):
            needs_backfill.append(p)
        else:
            well_formed.append(p)

    print(f"Missing source (need backfill): {len(needs_backfill)}")
    print(f"Well-formed (have source): {len(well_formed)}")

    if dry_run:
        print(f"\n[DRY RUN] Would backfill {len(needs_backfill)} points.")
        print(f"[DRY RUN] Would sync FTS5 tags for {len(well_formed)} well-formed points.")
        return

    # --- Part A: Backfill missing-source points ---
    backfilled = 0
    for p in needs_backfill:
        pid = str(p["id"])
        payload = p.get("payload", {})

        # Build metadata to merge — only set fields that are absent
        updates: dict = {}
        if not payload.get("source"):
            # Normalize "session:uuid" oddball to "session_extraction"
            updates["source"] = "fts5_reindex"
        if not payload.get("confidence"):
            updates["confidence"] = 0.5
        if not payload.get("created_at"):
            updates["created_at"] = DEFAULT_CREATED_AT
        if not payload.get("scope"):
            updates["scope"] = "user"
        if not payload.get("tags"):
            # Try FTS5 first
            fts_tag_str = fts_tags.get(pid, "")
            updates["tags"] = fts_tag_str.split() if fts_tag_str else []
        if "source_type" not in payload:
            updates["source_type"] = "memory"

        if not updates:
            continue

        resp = client.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/payload",
            json={"payload": updates, "points": [pid]},
        )
        if resp.status_code == 200:
            backfilled += 1
        else:
            print(f"  Failed to update {pid}: {resp.status_code} {resp.text[:100]}")

    print(f"\nBackfilled {backfilled}/{len(needs_backfill)} points")

    # --- Part B: Sync FTS5 tags FROM well-formed Qdrant points ---
    # These 219 points have tags in Qdrant but FTS5 tags are empty
    # after the migration. Copy Qdrant tags → FTS5.
    synced = 0
    for p in well_formed:
        pid = str(p["id"])
        payload = p.get("payload", {})
        qdrant_tags = payload.get("tags") or []
        if not qdrant_tags:
            continue

        tag_str = " ".join(str(t) for t in qdrant_tags)
        try:
            # Update FTS5 — delete + reinsert (FTS5 doesn't support UPDATE)
            row = db.execute(
                "SELECT content, source_type, collection FROM memory_fts WHERE memory_id = ?",
                (pid,),
            ).fetchone()
            if row:
                db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (pid,))
                db.execute(
                    "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pid, row["content"], row["source_type"], tag_str, row["collection"]),
                )
                synced += 1
        except Exception as exc:
            print(f"  FTS5 sync failed for {pid}: {exc}")

    db.commit()
    db.close()
    print(f"Synced FTS5 tags for {synced}/{len(well_formed)} well-formed points")

    # --- Part C: Normalize oddball source values ---
    # Find "session:uuid" pattern and normalize to "session_extraction"
    normalized = 0
    for p in points:
        payload = p.get("payload", {})
        src = payload.get("source", "")
        if src.startswith("session:"):
            pid = str(p["id"])
            resp = client.post(
                f"{QDRANT_URL}/collections/{COLLECTION}/points/payload",
                json={"payload": {"source": "session_extraction"}, "points": [pid]},
            )
            if resp.status_code == 200:
                normalized += 1
    if normalized:
        print(f"Normalized {normalized} oddball source values to 'session_extraction'")

    # --- Verification ---
    verify_points = _scroll_all_points(client)
    still_missing = sum(1 for p in verify_points if not p.get("payload", {}).get("source"))
    print(f"\nVerification: {still_missing} points still missing source (should be 0)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill Qdrant metadata")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
