"""Pure core for the memory_class Qdrant re-sync (data repair for the
EmbeddingRecoveryWorker recompute regression).

Background: the recovery worker used to recompute ``memory_class`` from a
content heuristic on every re-embed, discarding the authoritative store-time
class (e.g. reference-extraction stores URL content as ``fact`` to dodge the
0.7x reference activation penalty). SQLite ``memory_metadata.memory_class``
stayed correct, but the Qdrant payload — which recall actually reads
(``retrieval.py`` / ``memory_core_facts``) — drifted. The worker itself is
fixed (it now restores the stored class); THIS backfill repairs the payloads a
past buggy re-embed already diverged (those points still have vectors, so the
reconcile lane's mirror requeue never revisits them).

Mirrors SQLite truth (authoritative) onto the vector payloads via a MERGING
payload set — vectors and all other payload fields untouched. Sync + blocking
(SQLite + Qdrant HTTP) BY DESIGN — the data-migration runner offloads it via
``asyncio.to_thread``.

Idempotent and safe to lag / re-run:
- a point whose payload class already matches SQLite is skipped (a second run =
  0 updates),
- a point with no SQLite metadata row is skipped — nothing authoritative to
  mirror; the worker's heuristic fallback already owns that case,
- "diverged" is defined against the EFFECTIVE read value
  ``payload.get("memory_class", "fact")`` so a missing key that already reads as
  the correct default is NOT rewritten (no pointless whole-corpus writes).
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict

from qdrant_client import QdrantClient

from genesis.qdrant.collections import scroll_points, set_payload_batch

COLLECTIONS = ("episodic_memory", "knowledge_base")

# What recall reads when a payload carries no memory_class key (see
# retrieval.py / activation.py ``payload.get("memory_class", "fact")``). A
# missing key is only "diverged" when SQLite disagrees with this default.
_READ_DEFAULT = "fact"


def _sqlite_classes(db: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """``memory_metadata.memory_class`` for the given point ids (authoritative).

    Only rows with a non-NULL class are returned; a point absent from the map
    has no authoritative class and is left untouched by the caller."""
    out: dict[str, str] = {}
    for i in range(0, len(ids), 900):  # SQLite bound-parameter headroom
        chunk = ids[i : i + 900]
        placeholders = ",".join("?" * len(chunk))
        rows = db.execute(
            f"SELECT memory_id, memory_class FROM memory_metadata "  # noqa: S608 - placeholders bound
            f"WHERE memory_id IN ({placeholders}) AND memory_class IS NOT NULL",
            chunk,
        ).fetchall()
        out.update(dict(rows))
    return out


def _diverged_in_collection(
    db: sqlite3.Connection, client: QdrantClient, collection: str
) -> dict[str, list[str]]:
    """Map target-class -> point ids whose Qdrant class disagrees with SQLite.

    Streams the whole collection; buffers only the (small) diverged set."""
    by_target: dict[str, list[str]] = defaultdict(list)
    offset: str | None = None
    while True:
        points, offset = scroll_points(client, collection=collection, limit=1000, offset=offset)
        ids = [p["id"] for p in points]
        sqlite_map = _sqlite_classes(db, ids) if ids else {}
        for p in points:
            authoritative = sqlite_map.get(p["id"])
            if authoritative is None:
                continue  # no metadata row -> nothing to mirror
            effective = (p.get("payload") or {}).get("memory_class", _READ_DEFAULT)
            if effective != authoritative:
                by_target[authoritative].append(p["id"])
        if offset is None:
            return dict(by_target)


def count_diverged_memory_class(db: sqlite3.Connection, client: QdrantClient) -> int:
    """Total points whose Qdrant memory_class disagrees with authoritative SQLite.

    The verify() signal: 0 means the re-sync is complete on this install."""
    return sum(
        sum(len(v) for v in _diverged_in_collection(db, client, c).values()) for c in COLLECTIONS
    )


def resync_memory_class(
    db: sqlite3.Connection, client: QdrantClient, *, dry_run: bool = False
) -> dict[str, int]:
    """Mirror authoritative SQLite ``memory_class`` onto diverged Qdrant payloads.

    Returns per-target-class repair counts (e.g. ``{"fact": 318, "rule": 1}``).
    ``db`` is any read connection over genesis.db; ``client`` a QdrantClient —
    both injected so a CLI shim and the data-migration share ONE body."""
    totals: Counter[str] = Counter()
    for collection in COLLECTIONS:
        by_target = _diverged_in_collection(db, client, collection)
        for target_class, class_ids in by_target.items():
            totals[target_class] += len(class_ids)
            if not dry_run:
                set_payload_batch(
                    client,
                    collection=collection,
                    point_ids=class_ids,
                    payload={"memory_class": target_class},
                )
    return dict(totals)
