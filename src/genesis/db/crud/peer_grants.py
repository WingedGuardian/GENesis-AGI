"""Reads of ``peer_grants``. Writes go through ``peers.registry``."""

from __future__ import annotations

import aiosqlite


async def decision(db: aiosqlite.Connection, peer_id: str, capability: str) -> str | None:
    """The grant decision (allow, ask or deny) for one capability, or None."""
    cursor = await db.execute(
        "SELECT decision FROM peer_grants WHERE peer_id=? AND capability=?",
        (peer_id, capability),
    )
    row = await cursor.fetchone()
    return row[0] if row else None
