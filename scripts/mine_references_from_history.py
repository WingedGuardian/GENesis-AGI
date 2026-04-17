#!/usr/bin/env python3
"""Mine reference data (credentials, URLs, IPs) from historical transcripts.

Runs the LLM extraction pipeline in reference-only mode across past CC
sessions, promoting reference-shaped extractions into the reference store
via the classifier in ``genesis.memory.reference_extraction``. Does NOT
touch episodic memory or the extraction watermark — the normal 2h
extraction cycle is unaffected.

Cost: uses the ``9_fact_extraction`` call site which is SLM-tier
(mistral-free → groq-free → gemini-free → openrouter-free). Zero-dollar
per call; circuit-breaker quotas are the only rate-limiter.

Usage:
    source .venv/bin/activate
    python scripts/mine_references_from_history.py [--dry-run] [--sessions N] [--days N] [--start-line N]

Examples:
    # Dry-run: show which sessions would be mined without LLM calls
    python scripts/mine_references_from_history.py --dry-run --days 30

    # Mine the last 14 days, cap at 20 sessions (safe first run)
    python scripts/mine_references_from_history.py --sessions 20 --days 14

    # Mine EVERYTHING from scratch (slow, several hours via free-tier chain)
    python scripts/mine_references_from_history.py --days 999 --start-line 0
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
from dotenv import load_dotenv  # noqa: E402

from genesis.env import secrets_path  # noqa: E402

_secrets = secrets_path()
if _secrets.exists():
    load_dotenv(str(_secrets), override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("mine-references")


async def main(args: argparse.Namespace) -> None:
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

        sessions = await _find_extractable_sessions(db)

        if args.days:
            from datetime import UTC, datetime, timedelta

            cutoff = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
            sessions = [
                s for s in sessions
                if (s.get("started_at") or "") >= cutoff
            ]

        logger.info(
            "Found %d eligible sessions (last %d days)",
            len(sessions), args.days or 9999,
        )

        if args.sessions:
            sessions = sessions[: args.sessions]
            logger.info("Limited to %d sessions", len(sessions))

        if args.dry_run:
            for s in sessions:
                cc_id = s.get("cc_session_id") or s["id"]
                logger.info(
                    "  [DRY-RUN] Session %s (tag=%s, started=%s)",
                    cc_id[:12], s.get("source_tag", "?"),
                    (s.get("started_at") or "?")[:10],
                )
            logger.info(
                "Dry run complete. %d sessions would be mined (no LLM calls).",
                len(sessions),
            )
            return

        if not sessions:
            logger.info("No sessions to mine. Exiting.")
            return

        # Initialize extraction dependencies — same shape as
        # backfill_session_memories.py, kept minimal since we're not
        # writing episodic memory (reference_only_mode skips the
        # store.store() loop for episodic rows).
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
            logger.exception("Failed to initialize mining dependencies")
            return

        session_filter = {s["id"] for s in sessions}

        logger.info(
            "Starting reference-only extraction across %d sessions "
            "(start_line_override=%d, reference_only_mode=True)...",
            len(sessions), args.start_line,
        )
        summary = await run_extraction_cycle(
            db=db,
            store=store,
            router=router,
            linker=None,  # No typed links for reference-only runs
            transcript_dir=_TRANSCRIPT_DIR,
            reference_only_mode=True,
            start_line_override=args.start_line,
            session_filter=session_filter,
        )

        logger.info(
            "Mining complete: sessions=%d chunks=%d references_captured=%d "
            "errors=%d zero_entity_chunks=%d",
            summary["sessions_processed"],
            summary["chunks_processed"],
            summary["references_captured"],
            summary["errors"],
            summary["zero_entity_chunks"],
        )
        logger.info(
            "Production extraction state is UNCHANGED (watermarks not "
            "advanced; the next regular 2h cycle will still pick up "
            "these transcripts for episodic storage).",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Mine reference data from historical transcripts via the "
            "existing LLM extraction pipeline (SLM tier, free-tier chain)."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show which sessions would be mined without running LLM calls",
    )
    parser.add_argument(
        "--sessions", type=int, default=None,
        help="Maximum number of sessions to process (default: all in window)",
    )
    parser.add_argument(
        "--days", type=int, default=14,
        help="Number of days back to include (default: 14)",
    )
    parser.add_argument(
        "--start-line", type=int, default=0,
        help=(
            "Start line in each transcript. Default 0 (process from "
            "beginning). Overrides per-session last_extracted_line."
        ),
    )
    args = parser.parse_args()
    asyncio.run(main(args))
