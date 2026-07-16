#!/usr/bin/env python3
"""One-shot supervised Qdrant payload wing re-sync.

``memory_metadata.wing`` (SQLite) is authoritative, but embedded rows
classified before/outside the store-time payload write (historical backfills,
e.g. ``scripts/backfill_memory_taxonomy.py``) can carry a real wing in SQLite
while their Qdrant point payload's ``wing`` is missing or stale (``general``).
Qdrant applies wing as a hard ``must`` filter, so those points are excluded
from vector-path wing-filtered recall despite carrying a real wing.

This re-syncs the Qdrant payload from SQLite (one-shot, idempotent):

1. Scroll the points whose payload ``wing`` is missing or ``general``.
2. Look up the authoritative ``wing``/``room`` in ``memory_metadata``.
3. Where the SQLite wing is real (not NULL/``general``),
   ``set_payload({wing, room, life_domain})`` — merged onto the point,
   vectors and other payload keys untouched. ``life_domain`` is derived from
   the wing via the same mapping the write path uses.

Driven from Qdrant (not SQLite) so it only touches points that actually exist
— embedded-metadata rows with no Qdrant point are never dereferenced. There is
no runtime writer of ``memory_metadata.wing`` (only one-shot backfill scripts),
so this re-sync is durable, not a recurring patch.

Dry-run by default; ``--apply`` performs the writes. Idempotent: re-synced
points drop out of the scroll filter.

Closes the Qdrant half of follow-up 40d36a4e. The retrieval-side FTS fix ships
in the same PR.

Usage:
    python scripts/wing_payload_resync.py --sample 20   # dry-run a sample
    python scripts/wing_payload_resync.py               # dry-run everything
    python scripts/wing_payload_resync.py --apply       # supervised write
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

# Ensure genesis package is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

from genesis.env import secrets_path

logger = logging.getLogger("wing_payload_resync")

# Collections whose points carry a wing payload used by wing-filtered recall.
COLLECTIONS = ("episodic_memory", "knowledge_base")
SCROLL_PAGE = 1000
# SQLite has a variable limit (~999 default) on IN (...) — chunk below it.
SQLITE_IN_CHUNK = 900
# Values that are NOT a real wing (skip — the point legitimately has no wing).
_NON_WINGS = {None, "", "general"}


def scroll_stale_wing_ids(client, collection: str, cap: int | None = None) -> list[dict]:
    """Point records whose payload ``wing`` is missing or ``general``.

    Returns dicts ``{"id", "pwing", "proom"}`` (payload wing/room fetched so a
    dry-run can show the before-state). Vectors are never fetched.
    """
    from qdrant_client.models import (
        FieldCondition,
        Filter,
        IsEmptyCondition,
        MatchValue,
        PayloadField,
    )

    # should = OR: payload wing absent OR equal to the placeholder 'general'.
    stale_filter = Filter(
        should=[
            IsEmptyCondition(is_empty=PayloadField(key="wing")),
            FieldCondition(key="wing", match=MatchValue(value="general")),
        ]
    )

    out: list[dict] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=stale_filter,
            limit=SCROLL_PAGE,
            offset=offset,
            with_payload=["wing", "room"],
            with_vectors=False,
        )
        for p in points:
            payload = p.payload or {}
            out.append(
                {
                    "id": str(p.id),
                    "pwing": payload.get("wing"),
                    "proom": payload.get("room"),
                }
            )
        if offset is None or (cap is not None and len(out) >= cap):
            break
    return out[:cap] if cap is not None else out


def fetch_metadata_wings(conn, ids: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """memory_id -> (wing, room) from memory_metadata for the given ids.

    Batched IN queries under the SQLite variable limit. Missing ids are absent
    from the result (no Qdrant-only ghost gets a fabricated wing).
    """
    meta: dict[str, tuple[str | None, str | None]] = {}
    for start in range(0, len(ids), SQLITE_IN_CHUNK):
        chunk = ids[start : start + SQLITE_IN_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT memory_id, wing, room FROM memory_metadata "  # noqa: S608 - bound params
            f"WHERE memory_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for memory_id, wing, room in rows:
            meta[memory_id] = (wing, room)
    return meta


def plan_groups(
    stale: list[dict],
    meta: dict[str, tuple[str | None, str | None]],
) -> tuple[dict[tuple[str, str | None], list[str]], int, int]:
    """Group re-syncable point ids by (wing, room).

    Returns ``(groups, skipped_no_wing, missing_metadata)``. A point is
    re-syncable only when its SQLite wing is real (not NULL/empty/``general``).
    """
    groups: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    skipped_no_wing = 0
    missing_metadata = 0
    for rec in stale:
        mid = rec["id"]
        if mid not in meta:
            missing_metadata += 1
            continue
        wing, room = meta[mid]
        if wing in _NON_WINGS:
            skipped_no_wing += 1
            continue
        groups[(wing, room)].append(mid)
    return groups, skipped_no_wing, missing_metadata


def apply_resync(
    client,
    collection: str,
    groups: dict[tuple[str, str | None], list[str]],
    *,
    apply: bool,
) -> int:
    """set_payload({wing, room, life_domain}) per (wing, room) group.

    Returns the number of points written (or that WOULD be written in dry-run).
    """
    from genesis.memory.taxonomy import classify_life_domain
    from genesis.qdrant.collections import set_payload_batch

    written = 0
    for (wing, room), point_ids in groups.items():
        payload: dict = {"wing": wing, "life_domain": classify_life_domain(wing)}
        if room:
            payload["room"] = room
        if apply:
            set_payload_batch(client, collection=collection, point_ids=point_ids, payload=payload)
        written += len(point_ids)
    return written


def process_collection(
    client, conn, collection: str, *, cap: int | None, sample: int | None, apply: bool
) -> dict:
    """Scroll → plan → (optionally) write for one collection. Returns counts."""
    stale = scroll_stale_wing_ids(client, collection, cap=cap)
    if not stale:
        print(f"[{collection}] no points with missing/general wing.")
        return {"stale": 0, "synced": 0, "skipped_no_wing": 0, "missing_metadata": 0}

    ids = [r["id"] for r in stale]
    meta = fetch_metadata_wings(conn, ids)
    groups, skipped_no_wing, missing_metadata = plan_groups(stale, meta)
    resyncable = sum(len(v) for v in groups.values())

    print(
        f"[{collection}] stale={len(stale)} resyncable={resyncable} "
        f"skipped_no_wing={skipped_no_wing} missing_metadata={missing_metadata} "
        f"groups={len(groups)}"
    )

    if sample:
        shown = 0
        for rec in stale:
            mid = rec["id"]
            if mid not in meta:
                continue
            wing, room = meta[mid]
            if wing in _NON_WINGS:
                continue
            from genesis.memory.taxonomy import classify_life_domain

            print(
                f"  {mid[:8]}  payload.wing={rec['pwing']!r} -> "
                f"wing={wing!r} room={room!r} life_domain={classify_life_domain(wing)!r}"
            )
            shown += 1
            if shown >= sample:
                break

    synced = apply_resync(client, collection, groups, apply=apply)
    return {
        "stale": len(stale),
        "synced": synced,
        "skipped_no_wing": skipped_no_wing,
        "missing_metadata": missing_metadata,
    }


def main(args: argparse.Namespace) -> int:
    import sqlite3

    from genesis.env import genesis_db_path, qdrant_url

    load_dotenv(secrets_path())

    from qdrant_client import QdrantClient

    client = QdrantClient(url=qdrant_url(), timeout=30)
    # Read-only, WAL-aware connection (mode=ro sees un-checkpointed writes,
    # unlike immutable=1) — this script only READS SQLite; all writes go to Qdrant.
    conn = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        totals = {"stale": 0, "synced": 0, "skipped_no_wing": 0, "missing_metadata": 0}
        for collection in COLLECTIONS:
            counts = process_collection(
                client,
                conn,
                collection,
                cap=args.limit,
                sample=args.sample,
                apply=args.apply,
            )
            for k in totals:
                totals[k] += counts[k]

        if not args.apply:
            print(
                f"\nDRY RUN — no writes. TOTAL resyncable={totals['synced']} "
                f"across {len(COLLECTIONS)} collections. Re-run with --apply to write."
            )
        else:
            print(
                f"\nAPPLIED: {totals['synced']} points re-synced "
                f"(wing/room/life_domain merged onto payload); "
                f"{totals['skipped_no_wing']} skipped (no real SQLite wing); "
                f"{totals['missing_metadata']} had no metadata row."
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="cap points scanned per collection")
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="print up to N per-point before/after lines for review",
    )
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    raise SystemExit(main(parser.parse_args()))
