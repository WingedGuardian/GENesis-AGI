#!/usr/bin/env python3
"""One-time cleanup for Phase 1.5e: remove subsystem-tagged points from
Qdrant + backfill ``invalid_at`` on the corresponding metadata rows.

Background
----------
Before 1.5e, ego corrections, triage signals, and reflection observations
were dual-written to both SQLite and Qdrant. The 1.5e change makes those
writes FTS5+metadata only — no embedding, no vector index space. This
script cleans up the legacy state on installs that already have such
rows in Qdrant.

It also backfills ``memory_metadata.invalid_at`` from the linked
observation's ``expires_at`` so legacy rows acquire correct TTL going
forward. Rows whose source observation can't be linked (no ``obs:<uuid>``
tag, or the observation has been deleted) are left as NULL — they stay
accessible via ``only_subsystem`` until the user invalidates them.

Usage
-----
Always dry-run first to verify the scope:

    python scripts/cleanup_subsystem_qdrant.py            # dry-run
    python scripts/cleanup_subsystem_qdrant.py --apply    # commits

Re-running after ``--apply`` is safe: Qdrant deletes are idempotent;
the invalid_at backfill is gated on ``WHERE invalid_at IS NULL``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

# Ensure genesis is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_OBS_TAG = re.compile(r"obs:([0-9a-fA-F-]{8,})")

# Qdrant ``source_pipeline`` values produced by internal machine subsystems.
# Points carrying these are decisional/reflection output that must not surface
# in default recall. The SQLite-driven delete below covers those with a tagged
# ``memory_metadata`` row; the payload orphan sweep (Step 2b) covers legacy
# points embedded WITHOUT a metadata row (Qdrant-only orphans).
_LEAK_PIPELINES: frozenset[str] = frozenset({
    "reflection",
    "deep_reflection",
    "quality_calibration",
    "weekly_assessment",
    "surplus_promotion",
    "module:automaton_supervisor",
})


async def main(apply: bool = False) -> None:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.env import genesis_db_path, qdrant_url

    db_path = genesis_db_path()
    logger.info("Database: %s", db_path)
    logger.info("Qdrant: %s", qdrant_url())
    logger.info("Mode: %s", "APPLY (will modify)" if apply else "DRY-RUN")

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        # --- Step 1: collect subsystem-tagged rows -------------------------
        cursor = await db.execute(
            "SELECT memory_id, source_subsystem, collection, invalid_at "
            "FROM memory_metadata "
            "WHERE source_subsystem IS NOT NULL"
        )
        rows = await cursor.fetchall()
        if not rows:
            # No tagged rows to purge, but DO NOT return — the payload orphan
            # sweep (Step 2b) still needs to run: metadata-less Qdrant orphans
            # exist independently of any tagged metadata row (e.g. legacy
            # points embedded before memory_metadata existed).
            logger.info(
                "No subsystem-tagged rows found; running orphan sweep only.",
            )

        by_subsystem: dict[str, list[tuple[str, str, str | None]]] = {}
        for memory_id, subsystem, collection, invalid_at in rows:
            by_subsystem.setdefault(subsystem, []).append(
                (memory_id, collection, invalid_at),
            )

        for subsystem, items in by_subsystem.items():
            logger.info("  %s: %d rows", subsystem, len(items))
        logger.info("Total: %d rows", len(rows))

        # --- Step 2: Qdrant point cleanup ----------------------------------
        qdrant = QdrantClient(url=qdrant_url(), timeout=15)

        # Pre-cleanup point counts
        for coll in ("episodic_memory", "knowledge_base"):
            try:
                info = qdrant.get_collection(collection_name=coll)
                logger.info(
                    "Pre-cleanup %s point count: %d", coll, info.points_count,
                )
            except Exception as exc:
                logger.warning("Pre-cleanup count failed for %s: %s", coll, exc)

        deleted_count = 0
        for memory_id, _subsystem, collection, _invalid_at in rows:
            # Try the recorded collection first, then the other as a
            # defensive fallback (the collection column is mostly reliable
            # but historical inserts pre-#311 sometimes drifted).
            candidates = [collection or "episodic_memory"]
            if "episodic_memory" not in candidates:
                candidates.append("episodic_memory")
            if "knowledge_base" not in candidates:
                candidates.append("knowledge_base")

            for coll in candidates:
                try:
                    if apply:
                        from qdrant_client.models import PointIdsList
                        qdrant.delete(
                            collection_name=coll,
                            points_selector=PointIdsList(points=[memory_id]),
                        )
                        deleted_count += 1
                    break  # found, no need to try others
                except Exception as exc:
                    logger.debug(
                        "Qdrant delete miss for %s in %s: %s",
                        memory_id, coll, exc,
                    )

        if apply:
            logger.info("Qdrant point deletes issued: %d", deleted_count)
            # Post-cleanup point counts
            for coll in ("episodic_memory", "knowledge_base"):
                try:
                    info = qdrant.get_collection(collection_name=coll)
                    logger.info(
                        "Post-cleanup %s point count: %d",
                        coll, info.points_count,
                    )
                except Exception as exc:
                    logger.warning(
                        "Post-cleanup count failed for %s: %s", coll, exc,
                    )
        else:
            logger.info(
                "DRY-RUN: would delete up to %d Qdrant points across "
                "episodic_memory + knowledge_base",
                len(rows),
            )

        # --- Step 2b: payload-based orphan sweep ---------------------------
        # Step 2 only deletes points that have a tagged memory_metadata row.
        # Legacy machine-leak points embedded in Qdrant with NO metadata row
        # (orphans) would survive Step 2 and keep leaking into vector recall.
        # Delete them by payload: any episodic_memory point whose
        # source_pipeline is a known machine pipeline AND which has no
        # memory_metadata row. Verified: such orphans have no FTS5/SQLite copy
        # either, so deleting the vector loses no user-facing content.
        # Episodic-only by construction: reflection/autonomy/automaton are
        # episodic writes; knowledge_base holds ingested external knowledge,
        # never machine-subsystem output (mirrors the backfill's scope).
        cursor = await db.execute("SELECT memory_id FROM memory_metadata")
        known_ids = {r[0] for r in await cursor.fetchall()}
        orphan_ids: list[str] = []
        next_offset = None
        while True:
            points, next_offset = qdrant.scroll(
                collection_name="episodic_memory",
                limit=1000,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                pipeline = (point.payload or {}).get("source_pipeline")
                if pipeline in _LEAK_PIPELINES and str(point.id) not in known_ids:
                    orphan_ids.append(str(point.id))
            if next_offset is None:
                break

        logger.info(
            "Orphan sweep: %d machine-leak point(s) in Qdrant with no "
            "metadata row", len(orphan_ids),
        )
        if orphan_ids:
            if apply:
                from qdrant_client.models import PointIdsList
                for start in range(0, len(orphan_ids), 500):
                    qdrant.delete(
                        collection_name="episodic_memory",
                        points_selector=PointIdsList(
                            points=orphan_ids[start:start + 500],
                        ),
                    )
                logger.info("Orphan sweep: deleted %d point(s)", len(orphan_ids))
            else:
                logger.info(
                    "DRY-RUN: would delete %d orphan point(s) via payload sweep",
                    len(orphan_ids),
                )

        # --- Step 2c: reconcile embedding_status on purged rows ------------
        # Steps 2/2b delete the Qdrant vector but leave the metadata row's
        # embedding_status untouched — so a subsystem row that was 'embedded'
        # now claims a vector that no longer exists. That lie has a real
        # consequence: mark_superseded (store.py) reads
        # ``embedding_status != 'fts5_only'`` to decide whether to update the
        # Qdrant payload, and would issue a doomed update_payload on the
        # deleted point. Every source_subsystem-tagged row is FTS5-only by
        # invariant (store.py forces it on subsystem writes), and post-purge
        # none of them has a vector, so normalise them all to 'fts5_only'.
        # Idempotent (gated on != 'fts5_only'); matches the forward-write path.
        cursor = await db.execute(
            "SELECT COUNT(*) FROM memory_metadata "
            "WHERE source_subsystem IS NOT NULL "
            "AND embedding_status IS NOT 'fts5_only'"
        )
        stale_status = (await cursor.fetchone())[0]
        if apply:
            await db.execute(
                "UPDATE memory_metadata SET embedding_status = 'fts5_only' "
                "WHERE source_subsystem IS NOT NULL "
                "AND embedding_status IS NOT 'fts5_only'"
            )
            await db.commit()
            logger.info(
                "embedding_status reconcile: set %d subsystem row(s) to "
                "'fts5_only'", stale_status,
            )
        else:
            logger.info(
                "DRY-RUN: would set embedding_status='fts5_only' on %d "
                "subsystem row(s)", stale_status,
            )

        # --- Step 3: invalid_at backfill via obs:<uuid> tag JOIN -----------
        # Find subsystem rows that still have NULL invalid_at, parse the
        # obs:<uuid> tag from memory_fts.tags, JOIN observations.expires_at,
        # and write invalid_at where the source observation has expired.
        cursor = await db.execute(
            "SELECT mm.memory_id, mf.tags "
            "FROM memory_metadata mm "
            "LEFT JOIN memory_fts mf ON mm.memory_id = mf.memory_id "
            "WHERE mm.source_subsystem IS NOT NULL "
            "AND mm.invalid_at IS NULL"
        )
        candidates = list(await cursor.fetchall())
        logger.info(
            "invalid_at backfill candidates (subsystem rows with NULL "
            "invalid_at): %d", len(candidates),
        )

        backfilled = 0
        skipped_no_tag = 0
        skipped_no_obs = 0
        for memory_id, tags in candidates:
            if not tags:
                skipped_no_tag += 1
                continue
            m = _OBS_TAG.search(tags)
            if not m:
                skipped_no_tag += 1
                continue
            obs_id = m.group(1)
            obs_cursor = await db.execute(
                "SELECT expires_at FROM observations WHERE id = ?",
                (obs_id,),
            )
            obs_row = await obs_cursor.fetchone()
            if obs_row is None or obs_row[0] is None:
                skipped_no_obs += 1
                continue
            expires_at = obs_row[0]
            if apply:
                await db.execute(
                    "UPDATE memory_metadata SET invalid_at = ? "
                    "WHERE memory_id = ? AND invalid_at IS NULL",
                    (expires_at, memory_id),
                )
            backfilled += 1

        if apply:
            await db.commit()
            logger.info("Backfilled invalid_at on %d rows", backfilled)
        else:
            logger.info(
                "DRY-RUN: would backfill invalid_at on %d rows", backfilled,
            )
        logger.info(
            "Backfill skipped: %d missing obs:<uuid> tag, "
            "%d observation row not found/expires_at NULL",
            skipped_no_tag, skipped_no_obs,
        )

        if not apply:
            logger.info("")
            logger.info("DRY-RUN complete. Re-run with --apply to commit.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually apply changes. Default is dry-run.",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
