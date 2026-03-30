#!/usr/bin/env python3
"""Backfill memory extraction from recent session transcripts.

One-time script to extract entities/decisions/relationships from the
last N days of session transcripts. Uses the same extraction pipeline
as the periodic job.

Usage:
    python scripts/backfill_session_memories.py [--dry-run] [--limit N] [--days N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure genesis package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Load API keys before any LLM calls
from dotenv import load_dotenv

from genesis.env import secrets_path

_secrets = secrets_path()
if _secrets.exists():
    load_dotenv(str(_secrets), override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")


async def main(args: argparse.Namespace) -> None:
    """Run the backfill."""
    import aiosqlite

    from genesis.memory.extraction_job import (
        _TRANSCRIPT_DIR,
        _find_extractable_sessions,
        run_extraction_cycle,
    )

    db_path = Path.home() / "genesis" / "data" / "genesis.db"
    if not db_path.exists():
        logger.error("Database not found at %s", db_path)
        return

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row

        # Find eligible sessions
        sessions = await _find_extractable_sessions(db)

        # Filter by --days if specified
        if args.days:
            from datetime import UTC, datetime, timedelta
            cutoff = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
            sessions = [
                s for s in sessions
                if (s.get("started_at") or "") >= cutoff
            ]

        logger.info("Found %d eligible sessions (last %d days)", len(sessions), args.days or 9999)

        if args.limit:
            sessions = sessions[: args.limit]
            logger.info("Limited to %d sessions", len(sessions))

        if args.dry_run:
            for s in sessions:
                cc_id = s.get("cc_session_id") or s["id"]
                last_line = s.get("last_extracted_line") or 0
                logger.info(
                    "  [DRY-RUN] Session %s (tag=%s, last_line=%d)",
                    cc_id[:12], s.get("source_tag", "?"), last_line,
                )
            logger.info("Dry run complete. %d sessions would be processed.", len(sessions))
            return

        # Reset watermarks so extraction re-processes from the beginning
        for s in sessions:
            await db.execute(
                "UPDATE cc_sessions SET last_extracted_line = 0, last_extracted_at = NULL "
                "WHERE id = ?",
                (s["id"],),
            )
        await db.commit()
        logger.info("Reset watermarks for %d sessions", len(sessions))

        # Initialize extraction dependencies
        try:
            from qdrant_client import QdrantClient

            from genesis.env import qdrant_url
            from genesis.memory.embeddings import EmbeddingProvider
            from genesis.memory.linker import MemoryLinker
            from genesis.memory.store import MemoryStore
            from genesis.routing.circuit_breaker import CircuitBreakerRegistry
            from genesis.routing.config import load_config
            from genesis.routing.cost_tracker import CostTracker
            from genesis.routing.degradation import DegradationTracker
            from genesis.routing.litellm_delegate import LiteLLMDelegate
            from genesis.routing.router import Router

            qdrant = QdrantClient(url=qdrant_url(), timeout=5)
            embedding_provider = EmbeddingProvider()
            linker = MemoryLinker(qdrant_client=qdrant, db=db)
            store = MemoryStore(
                embedding_provider=embedding_provider,
                qdrant_client=qdrant,
                db=db,
                linker=linker,
            )

            config_path = Path.home() / "genesis" / "config" / "model_routing.yaml"
            if not config_path.exists():
                logger.error("Routing config not found at %s", config_path)
                return
            config = load_config(config_path)
            delegate = LiteLLMDelegate(config)
            breakers = CircuitBreakerRegistry(config.providers)
            cost_tracker = CostTracker(db=db)
            degradation = DegradationTracker()
            router = Router(
                config=config,
                breakers=breakers,
                cost_tracker=cost_tracker,
                degradation=degradation,
                delegate=delegate,
            )
        except Exception:
            logger.exception("Failed to initialize extraction dependencies")
            return

        # Run extraction
        logger.info("Starting extraction...")
        summary = await run_extraction_cycle(
            db=db,
            store=store,
            router=router,
            linker=linker,
            transcript_dir=_TRANSCRIPT_DIR,
        )

        logger.info(
            "Backfill complete: %d sessions, %d entities, %d errors, %d zero-entity chunks",
            summary["sessions_processed"],
            summary["entities_extracted"],
            summary["errors"],
            summary["zero_entity_chunks"],
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill memory extraction")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be processed without extracting",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of sessions to process",
    )
    parser.add_argument(
        "--days", type=int, default=14,
        help="Number of days to look back (default: 14)",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
