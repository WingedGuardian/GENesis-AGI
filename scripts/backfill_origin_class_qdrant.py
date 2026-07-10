#!/usr/bin/env python3
"""Backfill ``origin_class`` onto existing Qdrant payloads (WS-3 B0).

Migration 0053 backfilled SQLite (memory_metadata + knowledge_units); Qdrant
payloads of pre-existing points still lack the key. This script mirrors the
SQLite truth onto the vectors via ``set_payload`` (MERGES fields — vectors
and other payload fields untouched).

Safe to lag / re-run:
- Nothing reads the payload key in B0, and B1's reader falls back to SQLite
  and normalizes missing values fail-closed at gate time.
- Idempotent: points that already carry ``origin_class`` are skipped, so a
  second run reports 0 updates.
- SQLite is authoritative (post-0053 every row is classified); a Qdrant
  point with no metadata row falls back to deriving from its own payload
  (source_pipeline / source_subsystem / collection).

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/backfill_origin_class_qdrant.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from genesis.env import genesis_db_path, qdrant_url  # noqa: E402
from genesis.memory.provenance import derive_origin_class  # noqa: E402

QDRANT_URL = qdrant_url()
COLLECTIONS = ("episodic_memory", "knowledge_base")
BATCH = 500


def _scroll_missing(client: httpx.Client, collection: str) -> list[dict]:
    """Scroll all points, returning those WITHOUT origin_class."""
    points: list[dict] = []
    offset = None
    while True:
        body: dict = {
            "limit": 1000,
            "with_payload": ["origin_class", "source_pipeline", "source_subsystem"],
        }
        if offset is not None:
            body["offset"] = offset
        resp = client.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll", json=body,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        for p in result["points"]:
            if not (p.get("payload") or {}).get("origin_class"):
                points.append(p)
        offset = result.get("next_page_offset")
        if offset is None:
            return points


def _sqlite_classes(db: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """memory_metadata.origin_class for the given point ids (authoritative)."""
    out: dict[str, str] = {}
    for i in range(0, len(ids), 900):  # SQLite bound-parameter limit headroom
        chunk = ids[i : i + 900]
        placeholders = ",".join("?" * len(chunk))
        rows = db.execute(
            f"SELECT memory_id, origin_class FROM memory_metadata "  # noqa: S608 - IN-list placeholders; ids bound
            f"WHERE memory_id IN ({placeholders}) AND origin_class IS NOT NULL",
            chunk,
        ).fetchall()
        out.update(dict(rows))
    return out


def _set_payload(
    client: httpx.Client, collection: str, ids: list[str], origin: str,
) -> None:
    for i in range(0, len(ids), BATCH):
        resp = client.post(
            f"{QDRANT_URL}/collections/{collection}/points/payload?wait=true",
            json={"payload": {"origin_class": origin}, "points": ids[i : i + BATCH]},
        )
        resp.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    totals: Counter[str] = Counter()
    with httpx.Client(timeout=60) as client:
        for collection in COLLECTIONS:
            missing = _scroll_missing(client, collection)
            print(f"{collection}: {len(missing)} points missing origin_class")
            if not missing:
                continue
            ids = [str(p["id"]) for p in missing]
            sqlite_map = _sqlite_classes(db, ids)
            by_class: dict[str, list[str]] = defaultdict(list)
            for p in missing:
                pid = str(p["id"])
                payload = p.get("payload") or {}
                origin = sqlite_map.get(pid) or derive_origin_class(
                    source_pipeline=payload.get("source_pipeline"),
                    source_subsystem=payload.get("source_subsystem"),
                    collection=collection,
                )
                by_class[origin].append(pid)
            for origin, class_ids in sorted(by_class.items()):
                orphans = sum(1 for pid in class_ids if pid not in sqlite_map)
                print(
                    f"  {origin}: {len(class_ids)} points"
                    + (f" ({orphans} via payload fallback, no metadata row)" if orphans else "")
                )
                totals[origin] += len(class_ids)
                if not args.dry_run:
                    _set_payload(client, collection, class_ids, origin)
    db.close()
    verb = "would update" if args.dry_run else "updated"
    print(f"Done: {verb} {sum(totals.values())} points — {dict(totals)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
