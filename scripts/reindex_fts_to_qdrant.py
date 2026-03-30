#!/usr/bin/env python3
"""Re-index FTS5 memory entries into Qdrant with current embedding model.

Use this after switching embedding models (e.g., Q8_0 → fp16) to ensure
all vectors in Qdrant are from the same model variant.

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/reindex_fts_to_qdrant.py [--dry-run] [--batch-size 10]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

BATCH_SIZE = 5


def _genesis_db_path() -> Path:
    return importlib.import_module("genesis.env").genesis_db_path()


def _qdrant_api():
    module = importlib.import_module("genesis.qdrant.collections")
    return module.ensure_collections, module.get_client, module.upsert_point


async def main(
    db_path: str | None = None,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
):
    # Load secrets.env so API keys and OLLAMA_URL are available
    from dotenv import load_dotenv

    from genesis.env import secrets_path

    secrets = secrets_path()
    if secrets.is_file():
        load_dotenv(str(secrets), override=True)
        print(f"Loaded secrets from {secrets}")

    db = sqlite3.connect(str(db_path or _genesis_db_path()))
    db.row_factory = sqlite3.Row

    cursor = db.execute(
        "SELECT rowid, memory_id, content, source_type, tags, collection "
        "FROM memory_fts ORDER BY rowid"
    )
    rows = cursor.fetchall()
    print(f"Found {len(rows)} FTS5 entries to re-index")

    if not rows:
        print("Nothing to do.")
        return

    # Set up Qdrant
    ensure_collections, get_client, upsert_point = _qdrant_api()
    client = get_client()
    ensure_collections(client)

    # Set up embedding provider (uses backend chain from env config)
    EmbeddingProvider = importlib.import_module(  # noqa: N806
        "genesis.memory.embeddings"
    ).EmbeddingProvider
    embedder = EmbeddingProvider(cache_dir=None)

    backend_names = [b.name for b in embedder._backends]
    print(f"Embedding backends: {backend_names}")

    if not backend_names:
        print("ERROR: No embedding backends configured. Check secrets.env.")
        return

    if dry_run:
        print(f"[DRY RUN] Would re-embed {len(rows)} entries. Exiting.")
        return

    success = 0
    failed = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        texts = [row["content"] for row in batch]

        try:
            vectors = await embedder.embed_batch(texts)
        except Exception as exc:
            print(f"  Embedding batch {i//batch_size + 1} failed: {exc}")
            failed += len(batch)
            continue

        for row, vec in zip(batch, vectors, strict=True):
            collection = row["collection"] or "episodic_memory"
            if collection not in ("episodic_memory", "knowledge_base"):
                collection = "episodic_memory"

            point_id = row["memory_id"] or str(uuid.uuid4())

            now_iso = datetime.now(UTC).isoformat()
            raw_tags = (row["tags"] or "").strip()
            tag_list = raw_tags.split() if raw_tags else []

            payload = {
                "content": row["content"],
                "source_type": row["source_type"] or "memory",
                "memory_type": "episodic",
                "origin": "fts5_reindex",
                "source": "fts5_reindex",
                "tags": tag_list,
                "confidence": 0.5,
                "created_at": now_iso,
                "retrieved_count": 0,
                "scope": "external" if collection == "knowledge_base" else "user",
            }

            try:
                upsert_point(
                    client,
                    collection=collection,
                    point_id=point_id,
                    vector=vec,
                    payload=payload,
                )
                success += 1
            except Exception as exc:
                print(f"  Upsert failed for {point_id}: {exc}")
                failed += 1

        print(
            f"  Batch {i//batch_size + 1}/{(len(rows) + batch_size - 1)//batch_size}: "
            f"{len(batch)} entries processed"
        )

    print(f"\nDone: {success} succeeded, {failed} failed")

    get_collection_info = importlib.import_module(
        "genesis.qdrant.collections"
    ).get_collection_info
    for coll in ("episodic_memory", "knowledge_base"):
        try:
            info = get_collection_info(client, coll)
            print(f"  {coll}: {info['points_count']} points")
        except Exception:
            pass

    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-index all FTS5 memories into Qdrant with current embedding model"
    )
    parser.add_argument("--db-path", default=None, help="Path to genesis.db")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()
    asyncio.run(main(db_path=args.db_path, batch_size=args.batch_size, dry_run=args.dry_run))
