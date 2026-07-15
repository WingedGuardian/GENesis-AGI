"""Pure core for the origin_class Qdrant backfill (WS-3 B0).

Migration 0053 backfilled ``origin_class`` onto SQLite (memory_metadata +
knowledge_units); Qdrant payloads of pre-existing points still lack the key.
This mirrors the SQLite truth (authoritative post-0053) onto the vectors via a
MERGING payload set — vectors and other payload fields untouched.

Extracted from the old ``scripts/backfill_origin_class_qdrant.py`` so BOTH the
CLI shim AND the data-migration ``d0001`` call ONE implementation (no fork).
Sync + blocking (SQLite + Qdrant HTTP) BY DESIGN — the data-migration runner
offloads it via ``asyncio.to_thread``; the CLI runs it directly.

Idempotent and safe to lag / re-run:
- points already carrying ``origin_class`` are skipped (a second run = 0 updates),
- a Qdrant point with no SQLite metadata row derives from its own payload,
- nothing reads the key destructively; a stale install self-heals on next run.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict

from qdrant_client import QdrantClient

from genesis.memory.provenance import derive_origin_class
from genesis.qdrant.collections import scroll_points, set_payload_batch

COLLECTIONS = ("episodic_memory", "knowledge_base")


def _scroll_missing(client: QdrantClient, collection: str) -> list[dict]:
    """All points in ``collection`` lacking a non-empty ``origin_class``."""
    missing: list[dict] = []
    offset: str | None = None
    while True:
        points, offset = scroll_points(client, collection=collection, limit=1000, offset=offset)
        for p in points:
            if not (p.get("payload") or {}).get("origin_class"):
                missing.append(p)
        if offset is None:
            return missing


def _sqlite_classes(db: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """``memory_metadata.origin_class`` for the given point ids (authoritative)."""
    out: dict[str, str] = {}
    for i in range(0, len(ids), 900):  # SQLite bound-parameter headroom
        chunk = ids[i : i + 900]
        placeholders = ",".join("?" * len(chunk))
        rows = db.execute(
            f"SELECT memory_id, origin_class FROM memory_metadata "  # noqa: S608 - placeholders bound
            f"WHERE memory_id IN ({placeholders}) AND origin_class IS NOT NULL",
            chunk,
        ).fetchall()
        out.update(dict(rows))
    return out


def count_missing_origin_class(client: QdrantClient) -> int:
    """Total points across both collections still lacking ``origin_class``.

    The verify() signal: 0 means the backfill is complete on this install."""
    return sum(len(_scroll_missing(client, c)) for c in COLLECTIONS)


def backfill_origin_class(
    db: sqlite3.Connection, client: QdrantClient, *, dry_run: bool = False
) -> dict[str, int]:
    """Mirror SQLite ``origin_class`` onto Qdrant payloads. Returns per-class counts.

    ``db`` is any read connection over genesis.db; ``client`` a QdrantClient.
    Both are injected so the CLI (its own ro conn) and d0001 (the same) share
    this body without either owning connection setup."""
    totals: Counter[str] = Counter()
    for collection in COLLECTIONS:
        missing = _scroll_missing(client, collection)
        if not missing:
            continue
        ids = [p["id"] for p in missing]
        sqlite_map = _sqlite_classes(db, ids)
        by_class: dict[str, list[str]] = defaultdict(list)
        for p in missing:
            pid = p["id"]
            payload = p.get("payload") or {}
            origin = sqlite_map.get(pid) or derive_origin_class(
                source_pipeline=payload.get("source_pipeline"),
                source_subsystem=payload.get("source_subsystem"),
                collection=collection,
            )
            by_class[origin].append(pid)
        for origin, class_ids in by_class.items():
            totals[origin] += len(class_ids)
            if not dry_run:
                set_payload_batch(
                    client,
                    collection=collection,
                    point_ids=class_ids,
                    payload={"origin_class": origin},
                )
    return dict(totals)
