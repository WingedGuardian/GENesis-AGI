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

        Deduplicates against existing pending follow-ups from ego_cycle
        source to prevent the accumulation bug where the ego re-outputs
        follow-ups it already sees in its context.

        Returns count of genuinely new follow-ups stored.
        """
        # Fetch existing pending ego follow-ups for dedup comparison.
        existing = await follow_up_crud.get_pending(
            self._db, source="ego_cycle",
        )
        existing_contents = {row["content"].strip().lower() for row in existing}

        count = 0
        for text in follow_ups:
            if not text or not text.strip():
                continue
            normalized = text.strip()
            if normalized.lower() in existing_contents:
                logger.debug(
                    "Skipping duplicate follow-up from cycle %s: %.80s",
                    cycle_id, normalized,
                )
                continue
            await follow_up_crud.create(
                self._db,
                content=normalized,
                source="ego_cycle",
                source_session=cycle_id,
                strategy="ego_judgment",
                reason=f"Ego cycle {cycle_id} identified this as an open thread",
            )
            # Track the new entry so subsequent items in this batch dedup too.
            existing_contents.add(normalized.lower())
            count += 1
        if count:
            logger.info("Recorded %d new follow-ups from ego cycle %s", count, cycle_id)
        return count

    async def resolve_follow_ups(
        self,
        resolved: list[dict],
        cycle_id: str,
    ) -> int:
        """Mark follow-ups as resolved by the ego.

        Each item in `resolved` should have 'id' and 'resolution' keys.
        Returns count of successfully resolved follow-ups.
        """
        count = 0
        for item in resolved:
            if not isinstance(item, dict):
                continue
            fid = item.get("id", "")
            resolution = item.get("resolution", "Resolved by ego")
            if not fid:
                continue
            # Pinned follow-ups cannot be auto-resolved by ego — only the
            # user can close them.  Ego can still report on them but the
            # status transition is blocked here.
            existing = await follow_up_crud.get_by_id(self._db, fid)
            if existing and existing.get("pinned"):
                logger.info(
                    "Follow-up %s is pinned — ego cannot auto-resolve (cycle %s)",
                    fid, cycle_id,
                )
                continue
            ok = await follow_up_crud.update_status(
                self._db,
                fid,
                "completed",
                resolution_notes=f"Ego cycle {cycle_id}: {resolution}",
            )
            if ok:
                count += 1
            else:
                logger.warning(
                    "Follow-up %s not found for resolution (cycle %s)",
                    fid, cycle_id,
                )
        if count:
            logger.info("Resolved %d follow-ups from ego cycle %s", count, cycle_id)
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
