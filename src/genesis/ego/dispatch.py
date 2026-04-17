"""Ego follow-up dispatcher — records follow-ups in the accountability ledger.

Ego follow-ups are stored in the follow_ups table (not ego_state KV).
They persist until resolved — NOT cleared each cycle. The ego sees
pending/failed follow-ups in its context and can act on them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.db.crud import follow_ups as follow_up_crud

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


class EgoDispatcher:
    """Records ego follow-up requests in the follow_ups accountability table.

    Follow-ups persist until resolved — the ego sees them in context each cycle
    and can act on pending/failed items. No more clearing each cycle.
    """

    def __init__(self, *, db: aiosqlite.Connection) -> None:
        self._db = db

    async def record_follow_ups(
        self,
        follow_ups: list[str],
        cycle_id: str,
    ) -> int:
        """Store follow-ups from an ego cycle in the accountability ledger.

        Unlike the old KV approach, follow-ups accumulate until resolved.
        Each follow-up gets strategy='ego_judgment' so the dispatcher leaves
        them for the ego to evaluate.

        Returns count stored.
        """
        count = 0
        for text in follow_ups:
            if not text or not text.strip():
                continue
            await follow_up_crud.create(
                self._db,
                content=text.strip(),
                source="ego_cycle",
                source_session=cycle_id,
                strategy="ego_judgment",
                reason=f"Ego cycle {cycle_id} identified this as an open thread",
            )
            count += 1
        if count:
            logger.info("Recorded %d follow-ups from ego cycle %s", count, cycle_id)
        return count

    async def get_pending_follow_ups(self) -> list[dict]:
        """List follow-ups from ego cycles that are still actionable.

        Returns pending, failed, and blocked follow-ups from ego source.
        """
        all_actionable = await follow_up_crud.get_actionable(self._db)
        # Return all actionable follow-ups (not just ego-sourced), since
        # the ego should see the full picture of what's pending.
        return all_actionable

    async def clear_follow_up(self, follow_up_id: str) -> None:
        """Mark a follow-up as completed by the ego."""
        await follow_up_crud.update_status(
            self._db, follow_up_id, "completed",
            resolution_notes="Resolved by ego cycle",
        )
