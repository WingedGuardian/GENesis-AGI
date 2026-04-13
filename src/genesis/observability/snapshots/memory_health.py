"""Memory health metrics snapshot — calls genesis.memory.health functions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_EK_PATH = Path.home() / ".genesis" / "essential_knowledge.md"


async def _wing_distribution(db: aiosqlite.Connection) -> list[tuple[str, int]]:
    """Top wings by memory count."""
    try:
        cursor = await db.execute(
            "SELECT wing, COUNT(*) AS cnt FROM memory_metadata "
            "WHERE wing IS NOT NULL GROUP BY wing ORDER BY cnt DESC LIMIT 8"
        )
        return [(row[0], row[1]) for row in await cursor.fetchall()]
    except Exception:
        logger.debug("wing distribution query failed", exc_info=True)
        return []


async def _class_distribution(db: aiosqlite.Connection) -> list[tuple[str, int]]:
    """Memory class breakdown."""
    try:
        cursor = await db.execute(
            "SELECT memory_class, COUNT(*) AS cnt FROM memory_metadata "
            "WHERE memory_class IS NOT NULL GROUP BY memory_class ORDER BY cnt DESC"
        )
        return [(row[0], row[1]) for row in await cursor.fetchall()]
    except Exception:
        logger.debug("class distribution query failed", exc_info=True)
        return []


async def _confidence_distribution(db: aiosqlite.Connection) -> dict[str, int]:
    """Count memories by confidence bucket: high (>=0.7), medium (>=0.4), low (<0.4)."""
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM memory_metadata WHERE confidence >= 0.7"
        )
        high = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM memory_metadata "
            "WHERE confidence >= 0.4 AND confidence < 0.7"
        )
        medium = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM memory_metadata WHERE confidence < 0.4"
        )
        low = (await cursor.fetchone())[0]
        return {"high": high, "medium": medium, "low": low}
    except Exception:
        logger.debug("confidence distribution query failed", exc_info=True)
        return {}


async def _link_distribution(db: aiosqlite.Connection) -> dict:
    """Link count and top types."""
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM memory_links")
        total = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT link_type, COUNT(*) AS cnt FROM memory_links "
            "GROUP BY link_type ORDER BY cnt DESC LIMIT 5"
        )
        by_type = [(row[0], row[1]) for row in await cursor.fetchall()]
        return {"total": total, "by_type": by_type}
    except Exception:
        logger.debug("link distribution query failed", exc_info=True)
        return {"total": 0, "by_type": []}


async def _extraction_coverage(db: aiosqlite.Connection) -> dict:
    """Session extraction pipeline coverage."""
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM cc_sessions")
        total = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM cc_sessions WHERE last_extracted_line > 0"
        )
        extracted = (await cursor.fetchone())[0]
        pct = round(extracted / total * 100, 1) if total else 0.0
        return {"total_sessions": total, "extracted": extracted, "coverage_pct": pct}
    except Exception:
        logger.debug("extraction coverage query failed", exc_info=True)
        return {}


async def _code_index_stats(db: aiosqlite.Connection) -> dict:
    """Code index module/symbol counts and freshness."""
    try:
        cursor = await db.execute(
            "SELECT COUNT(*), MAX(last_indexed_at) FROM code_modules"
        )
        row = await cursor.fetchone()
        modules = row[0] if row else 0
        last_indexed = row[1] if row else None

        cursor = await db.execute("SELECT COUNT(*) FROM code_symbols")
        symbols = (await cursor.fetchone())[0]

        age_hours = None
        if last_indexed:
            try:
                indexed_dt = datetime.fromisoformat(last_indexed)
                if indexed_dt.tzinfo is None:
                    indexed_dt = indexed_dt.replace(tzinfo=UTC)
                age_hours = round(
                    (datetime.now(UTC) - indexed_dt).total_seconds() / 3600, 1
                )
            except (ValueError, TypeError):
                pass

        return {
            "modules": modules,
            "symbols": symbols,
            "last_indexed": last_indexed,
            "age_hours": age_hours,
        }
    except Exception:
        logger.debug("code index stats query failed", exc_info=True)
        return {}


def _essential_knowledge_stats() -> dict:
    """EK file age and size."""
    try:
        if not _EK_PATH.exists():
            return {"age_hours": None, "size_bytes": 0}
        stat = _EK_PATH.stat()
        age_hours = round(
            (datetime.now(UTC).timestamp() - stat.st_mtime) / 3600, 1
        )
        return {"age_hours": age_hours, "size_bytes": stat.st_size}
    except Exception:
        return {"age_hours": None, "size_bytes": 0}


async def memory_health(db: aiosqlite.Connection | None) -> dict:
    """Return algorithmic memory health stats for the dashboard.

    Calls the pure-SQL functions from genesis.memory.health plus
    structural queries for wings, classes, confidence, links,
    extraction coverage, code index, and essential knowledge.
    """
    if db is None:
        return {"status": "unavailable"}

    try:
        from genesis.memory.health import (
            distribution_stats,
            growth_stats,
            orphan_stats,
        )

        orphans = await orphan_stats(db)
        distribution = await distribution_stats(db)
        growth = await growth_stats(db)

        # Structural queries — all lightweight SQL aggregates
        wings = await _wing_distribution(db)
        classes = await _class_distribution(db)
        confidence = await _confidence_distribution(db)
        links = await _link_distribution(db)
        extraction = await _extraction_coverage(db)
        code_index = await _code_index_stats(db)
        ek = _essential_knowledge_stats()

        # Derive health status
        status = "healthy"
        if "error" in orphans or "error" in distribution or "error" in growth:
            status = "degraded"
        elif orphans.get("orphan_pct", 0) > 50:
            status = "warning"

        return {
            "status": status,
            "orphans": orphans,
            "distribution": distribution,
            "growth": growth,
            "wings": wings,
            "classes": classes,
            "confidence": confidence,
            "links": links,
            "extraction": extraction,
            "code_index": code_index,
            "essential_knowledge": ek,
        }
    except ImportError:
        logger.warning("genesis.memory.health not available", exc_info=True)
        return {"status": "unavailable"}
    except Exception:
        logger.error("Memory health snapshot failed", exc_info=True)
        return {"status": "error"}
