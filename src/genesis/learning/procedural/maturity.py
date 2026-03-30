"""Procedural memory maturity stage classification."""

from __future__ import annotations

import aiosqlite

from genesis.learning.types import MaturityStage


async def get_maturity_stage(db: aiosqlite.Connection) -> MaturityStage:
    """Count non-deprecated procedures and return maturity stage.

    EARLY: <50, GROWING: 50-200, MATURE: >200.
    """
    cursor = await db.execute(
        "SELECT COUNT(*) FROM procedural_memory WHERE deprecated = 0"
    )
    row = await cursor.fetchone()
    count = row[0] if row else 0

    if count < 50:
        return MaturityStage.EARLY
    if count <= 200:
        return MaturityStage.GROWING
    return MaturityStage.MATURE
