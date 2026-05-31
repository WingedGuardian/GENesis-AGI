"""Add reflection_corpus table for recording prompt I/O pairs.

Captures the full prompt text and raw LLM response for every reflection
dispatch (Micro, Light, Deep, Strategic).  Records are written BEFORE
storage gates (salience, dedup, cooldown) so we have visibility into
what the pipeline produces regardless of what gets stored.

Primary consumer: DSPy prompt optimization (future).  Immediate value:
observability and quality measurement via the reflection_quality rubric.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS reflection_corpus (
            id TEXT PRIMARY KEY,
            depth TEXT NOT NULL,
            focus_area TEXT,
            prompt_text TEXT NOT NULL,
            response_text TEXT NOT NULL,
            parsed_ok INTEGER,
            model_used TEXT,
            quality_score REAL,
            quality_label TEXT,
            graded_at TEXT,
            tick_id TEXT,
            created_at TEXT NOT NULL,
            used_in_optimization INTEGER DEFAULT 0
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_corpus_depth "
        "ON reflection_corpus(depth)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_corpus_quality "
        "ON reflection_corpus(quality_label)"
    )
