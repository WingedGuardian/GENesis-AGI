#!/usr/bin/env python3
"""Re-embed memories that got Mistral vectors during the Ollama outage.

Run AFTER deploying the cloud-primary embedding fix and restarting the bridge.
This queues affected memories into pending_embeddings for the background
EmbeddingRecoveryWorker to re-embed with the correct model.

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/reembed_mistral_vectors.py [--dry-run]
"""

from __future__ import annotations

import asyncio
import sys

# Outage window: Ollama started failing at 18:37 UTC on 2026-03-25
OUTAGE_START = "2026-03-25T18:37:00+00:00"


async def main(dry_run: bool = False) -> None:
    from genesis.db.connection import get_db
    from genesis.env import genesis_db_path

    db_path = genesis_db_path()
    print(f"Database: {db_path}")

    async with get_db(db_path) as db:
        # Find embedding.fallback events during the outage
        cursor = await db.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE event_type='embedding.fallback' AND created_at > ?",
            (OUTAGE_START,),
        )
        row = await cursor.fetchone()
        fallback_count = row[0] if row else 0
        print(f"Mistral fallback events since outage: {fallback_count}")

        # Find memories with Qdrant upserts during the window
        # These went through store.py which calls embed() then upsert_point()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM activity_log "
            "WHERE provider='qdrant.upsert' AND created_at > ?",
            (OUTAGE_START,),
        )
        row = await cursor.fetchone()
        upsert_count = row[0] if row else 0
        print(f"Qdrant upserts since outage: {upsert_count}")

        # Get memory IDs from pending_embeddings that were marked 'embedded'
        # during the outage — these got Mistral vectors
        cursor = await db.execute(
            "SELECT memory_id, substr(content, 1, 60), collection "
            "FROM pending_embeddings "
            "WHERE status='embedded' AND embedded_at > ?",
            (OUTAGE_START,),
        )
        embedded_during_outage = await cursor.fetchall()

        if embedded_during_outage:
            print(f"\nFound {len(embedded_during_outage)} memories embedded during outage:")
            for mid, preview, collection in embedded_during_outage:
                print(f"  {mid[:12]}... [{collection}] {preview}")

            if not dry_run:
                # Reset their status to 'pending' for re-embedding
                await db.execute(
                    "UPDATE pending_embeddings SET status='pending', embedded_at=NULL "
                    "WHERE status='embedded' AND embedded_at > ?",
                    (OUTAGE_START,),
                )
                await db.commit()
                print(f"\nReset {len(embedded_during_outage)} entries to 'pending'")
        else:
            print("\nNo pending_embeddings entries found from outage window.")
            print("Memories may have gone directly through store.py → Qdrant.")
            print("The background EmbeddingRecoveryWorker will handle these")
            print("on the next recovery cycle.")

        if dry_run:
            print("\n[DRY RUN] No changes made.")
        else:
            print("\nDone. The EmbeddingRecoveryWorker will re-embed these on next cycle.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    asyncio.run(main(dry_run=dry_run))
