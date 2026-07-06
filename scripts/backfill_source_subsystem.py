#!/usr/bin/env python3
"""Backfill ``memory_metadata.source_subsystem`` for legacy machine writes.

Background
----------
PRs #315/#317 added the ``source_subsystem`` column + default-exclude recall
filter, but legacy machine-generated memories (written before the tagging
code landed) still have ``source_subsystem = NULL``. They remain embedded in
Qdrant and leak into default semantic recall.

This script attributes each legacy row from its Qdrant ``source_pipeline``
payload and backfills the SQLite tag so default recall (and the FTS5 path)
excludes it. It pairs with ``cleanup_subsystem_qdrant.py``, which deletes the
now-tagged points from Qdrant (run the backfill FIRST, then the cleanup).

Attribution map (source_pipeline -> source_subsystem):
    reflection | deep_reflection | quality_calibration |
    weekly_assessment | surplus_promotion            -> "reflection"
    module:automaton_supervisor                       -> "autonomy"

Everything else is left untouched (NULL) — user-sourced content
(session_observer, harvest, conversation, ...) must stay in default recall.
Note: ``module:automaton_supervisor`` is a retired *module* (external, never a
Genesis subsystem); its decisional output folds into the ``autonomy`` bucket
purely to drop it out of recall — no new subsystem vocabulary is introduced.

Join key
--------
Qdrant point id == ``memory_metadata.memory_id``. Do NOT use the Qdrant
payload's own ``memory_id`` field — it is null on these rows.

Usage
-----
Always dry-run first to verify the scope:

    python scripts/backfill_source_subsystem.py            # dry-run
    python scripts/backfill_source_subsystem.py --apply    # commits

Idempotent: the UPDATE is gated on ``source_subsystem IS NULL``, so already
tagged rows (and re-runs) are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

# Ensure genesis is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Qdrant source_pipeline -> source_subsystem attribution.
_PIPELINE_TO_SUBSYSTEM: dict[str, str] = {
    "reflection": "reflection",
    "deep_reflection": "reflection",
    "quality_calibration": "reflection",
    "weekly_assessment": "reflection",
    "surplus_promotion": "reflection",
    "module:automaton_supervisor": "autonomy",
}


async def main(apply: bool = False) -> None:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.env import genesis_db_path, qdrant_url

    db_path = genesis_db_path()
    logger.info("Database: %s", db_path)
    logger.info("Qdrant: %s", qdrant_url())
    logger.info("Mode: %s", "APPLY (will modify)" if apply else "DRY-RUN")

    # --- Step 1: collect attributable point ids from Qdrant payloads -------
    qdrant = QdrantClient(url=qdrant_url(), timeout=30)
    point_subsystem: dict[str, str] = {}   # point_id -> subsystem
    pipeline_counts: Counter[str] = Counter()
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
            subsystem = _PIPELINE_TO_SUBSYSTEM.get(pipeline)
            if subsystem is not None:
                point_subsystem[str(point.id)] = subsystem
                pipeline_counts[pipeline] += 1
        if next_offset is None:
            break

    logger.info("Attributable Qdrant points: %d", len(point_subsystem))
    for pipeline, count in pipeline_counts.most_common():
        logger.info("  %-32s %d", pipeline, count)

    if not point_subsystem:
        logger.info("Nothing to backfill.")
        return

    # --- Step 2: join to memory_metadata + classify -----------------------
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("PRAGMA journal_mode=WAL")

        cursor = await db.execute(
            "SELECT memory_id, source_subsystem FROM memory_metadata",
        )
        current: dict[str, str | None] = {
            row[0]: row[1] for row in await cursor.fetchall()
        }

        to_update: list[tuple[str, str]] = []   # (subsystem, memory_id)
        already_tagged = 0
        no_metadata = 0
        by_subsystem: Counter[str] = Counter()
        for point_id, subsystem in point_subsystem.items():
            if point_id not in current:
                no_metadata += 1
                continue
            if current[point_id] is not None:
                already_tagged += 1
                continue
            to_update.append((subsystem, point_id))
            by_subsystem[subsystem] += 1

        logger.info("--- backfill classification ---")
        logger.info("  to tag (NULL -> subsystem): %d", len(to_update))
        for subsystem, count in by_subsystem.most_common():
            logger.info("    -> %-12s %d", subsystem, count)
        logger.info("  already tagged (skipped):   %d", already_tagged)
        logger.info(
            "  no metadata row (orphan; handled by cleanup sweep): %d",
            no_metadata,
        )

        if apply and to_update:
            await db.executemany(
                "UPDATE memory_metadata SET source_subsystem = ? "
                "WHERE memory_id = ? AND source_subsystem IS NULL",
                to_update,
            )
            await db.commit()
            logger.info("Backfilled source_subsystem on %d rows", len(to_update))
        elif not apply:
            logger.info(
                "DRY-RUN: would backfill source_subsystem on %d rows",
                len(to_update),
            )
            logger.info("DRY-RUN complete. Re-run with --apply to commit.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually apply changes. Default is dry-run.",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
