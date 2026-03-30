"""Ego follow-up dispatcher — records investigation requests for future dispatch.

Step 1: stores follow_ups in ego_state KV. Does NOT dispatch CC sessions.
Step 2 (future): dispatch investigation sessions via CC bridge.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.db.crud import ego as ego_crud

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_FOLLOW_UP_PREFIX = "follow_up:"


class EgoDispatcher:
    """Records ego follow-up requests for future dispatch.

    In Step 1 (proposal mode), the ego outputs ``follow_ups`` — open threads
    it wants to revisit next cycle. This class stores them in ego_state KV
    for retrieval during context assembly.

    Actual CC session dispatch is deferred to Step 2 (after basic ego loop
    is proven).
    """

    def __init__(self, *, db: aiosqlite.Connection) -> None:
        self._db = db

    async def clear_all_follow_ups(self) -> int:
        """Remove all follow_up entries from ego_state. Returns count removed."""
        cursor = await self._db.execute(
            "DELETE FROM ego_state WHERE key LIKE ?",
            (f"{_FOLLOW_UP_PREFIX}%",),
        )
        await self._db.commit()
        return cursor.rowcount

    async def record_follow_ups(
        self,
        follow_ups: list[str],
        cycle_id: str,
    ) -> int:
        """Store follow_ups in ego_state, replacing any prior follow_ups.

        Old follow_ups are cleared first to prevent unbounded accumulation.
        Returns count stored.
        """
        await self.clear_all_follow_ups()
        now = datetime.now(UTC).isoformat()
        count = 0
        for text in follow_ups:
            if not text or not text.strip():
                continue
            key = f"{_FOLLOW_UP_PREFIX}{uuid.uuid4().hex[:12]}"
            payload = json.dumps({
                "text": text.strip(),
                "cycle_id": cycle_id,
                "created_at": now,
            })
            await ego_crud.set_state(self._db, key=key, value=payload)
            count += 1
        if count:
            logger.info("Recorded %d follow_ups from cycle %s", count, cycle_id)
        return count

    async def get_pending_follow_ups(self) -> list[dict]:
        """List all pending follow_ups.

        Returns list of dicts with keys: key, text, cycle_id, created_at.
        """
        rows = await self._db.execute_fetchall(
            "SELECT key, value FROM ego_state WHERE key LIKE ?",
            (f"{_FOLLOW_UP_PREFIX}%",),
        )
        results = []
        for row in rows:
            try:
                data = json.loads(row[1])
                data["key"] = row[0]
                results.append(data)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid follow_up payload for key %s", row[0])
        return results

    async def clear_follow_up(self, key: str) -> None:
        """Remove a resolved follow_up from ego_state."""
        await self._db.execute(
            "DELETE FROM ego_state WHERE key = ?", (key,),
        )
        await self._db.commit()
