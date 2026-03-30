"""Step 2.6 — Quarantine and expire speculative claims."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.db.crud import speculative

if TYPE_CHECKING:
    import aiosqlite


async def filter_speculative(items: list[dict]) -> list[dict]:
    """Remove items where speculative=1 from retrieval context."""
    return [item for item in items if not item.get("speculative", False)]


async def expire_stale_claims(db: aiosqlite.Connection) -> int:
    """Archive speculative claims past expiry with zero supporting evidence.

    Returns count of expired claims.
    """
    now = datetime.now(UTC).isoformat()
    claims = await speculative.list_active(db)
    expired_count = 0

    for claim in claims:
        if claim["hypothesis_expiry"] < now and claim.get("evidence_count", 0) == 0:
            await speculative.archive(db, claim["id"], archived_at=now)
            expired_count += 1

    return expired_count
