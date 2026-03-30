#!/usr/bin/env python3
"""One-time migration: AZ FAISS memory → Genesis Qdrant + FTS5.

Usage:
    python scripts/migrate_faiss_to_qdrant.py [--memory-dir PATH] [--dry-run]

Reads all documents from AZ's FAISS index, re-embeds via Genesis EmbeddingProvider,
and inserts into Qdrant episodic_memory collection + SQLite FTS5.

Prerequisites:
    - Qdrant running on localhost:6333
    - Ollama reachable via OLLAMA_URL (default: http://localhost:11434)
      or a Mistral API key in secrets.env
    - Genesis DB initialized (run pytest or create_all_tables first)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_faiss_documents(memory_dir: Path) -> list[dict]:
    """Load documents from FAISS index files.

    Reads index.faiss + index.pkl using LangChain FAISS loader.
    Returns list of {page_content, metadata} dicts.
    """
    faiss_path = memory_dir / "index.faiss"
    pkl_path = memory_dir / "index.pkl"
    if not faiss_path.exists() or not pkl_path.exists():
        logger.error("FAISS index files not found in %s", memory_dir)
        return []

    try:
        from langchain_community.vectorstores import FAISS
        from langchain_community.vectorstores.utils import DistanceStrategy
        from langchain_core.embeddings import Embeddings

        class DummyEmbeddings(Embeddings):
            """Minimal embedder just to load FAISS index."""

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 1024 for _ in texts]

            def embed_query(self, text: str) -> list[float]:
                return [0.0] * 1024

        db = FAISS.load_local(
            folder_path=str(memory_dir),
            embeddings=DummyEmbeddings(),
            allow_dangerous_deserialization=True,
            distance_strategy=DistanceStrategy.COSINE,
        )

        all_docs = []
        if hasattr(db, "docstore") and hasattr(db.docstore, "_dict"):
            for doc_id, doc in db.docstore._dict.items():
                all_docs.append(
                    {
                        "page_content": doc.page_content,
                        "metadata": {
                            **doc.metadata,
                            "id": doc.metadata.get("id", doc_id),
                        },
                    }
                )

        return all_docs

    except ImportError as e:
        logger.error("Failed to import FAISS/LangChain: %s", e)
        logger.error("Install: pip install faiss-cpu langchain-community")
        return []
    except Exception:
        logger.exception("Failed to load FAISS index")
        return []


async def migrate(
    memory_dir: Path,
    db_path: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Run the migration. Returns stats dict."""
    import aiosqlite

    from genesis.db.schema import create_all_tables
    from genesis.memory.az_adapter import doc_to_payload  # noqa: F401
    from genesis.memory.embeddings import EmbeddingProvider
    from genesis.memory.store import MemoryStore
    from genesis.qdrant.collections import ensure_collections, get_client

    stats = {"total": 0, "migrated": 0, "skipped": 0, "errors": 0}

    docs = load_faiss_documents(memory_dir)
    stats["total"] = len(docs)

    if not docs:
        logger.info("No documents to migrate")
        return stats

    logger.info("Found %d documents in FAISS index", len(docs))

    if dry_run:
        for doc in docs:
            area = doc["metadata"].get("area", "main")
            logger.info(
                "  [DRY RUN] Would migrate: %s (area=%s, %d chars)",
                doc["metadata"].get("id", "?"),
                area,
                len(doc["page_content"]),
            )
        stats["skipped"] = len(docs)
        return stats

    # Set up Genesis infrastructure
    qdrant = get_client()
    ensure_collections(qdrant)

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    await db.commit()

    embeddings = EmbeddingProvider()
    store = MemoryStore(
        embedding_provider=embeddings,
        qdrant_client=qdrant,
        db=db,
        linker=None,  # Skip auto-linking during migration
    )

    try:
        for i, doc in enumerate(docs):
            try:
                content = doc["page_content"]
                metadata = doc["metadata"]
                tags = metadata.get("tags", [])
                if isinstance(tags, str):
                    tags = [tags]
                area = metadata.get("area", "main")

                await store.store(
                    content,
                    source=f"faiss_migration:{area}",
                    memory_type="episodic",
                    tags=tags,
                    confidence=0.5,
                    auto_link=False,
                )

                stats["migrated"] += 1
                if (i + 1) % 10 == 0:
                    logger.info("  Migrated %d/%d", i + 1, len(docs))

            except Exception:
                logger.exception(
                    "  Failed to migrate doc %s",
                    doc["metadata"].get("id", "?"),
                )
                stats["errors"] += 1
    finally:
        await db.close()

    # Rename old FAISS files so they aren't re-loaded by AZ
    for suffix in ("index.faiss", "index.pkl"):
        src = memory_dir / suffix
        if src.exists():
            dst = memory_dir / f"{suffix}.bak"
            src.rename(dst)
            logger.info("Renamed %s → %s", src, dst)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate AZ FAISS memory to Genesis Qdrant"
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=Path.home() / "agent-zero" / "usr" / "memory" / "default",
        help="Path to AZ memory subdirectory (default: ~/agent-zero/usr/memory/default)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.home() / "genesis" / "data" / "genesis.db",
        help="Path to Genesis SQLite database",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be migrated"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    stats = asyncio.run(migrate(args.memory_dir, args.db_path, dry_run=args.dry_run))

    print("\nMigration complete:")
    print(f"  Total documents: {stats['total']}")
    print(f"  Migrated: {stats['migrated']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"  Errors: {stats['errors']}")


if __name__ == "__main__":
    main()
