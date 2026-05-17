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
        """No-op: ego follow-up creation is disabled.

        The ego has proposals for action and observations for noting things.
        Follow-ups are a user-facing tracking mechanism — only foreground
        sessions should create them. Ego-generated follow-ups accumulated
        at ~50/month with 84% stale/noise rate (audit 2026-05-16).

        The ego can still READ follow-ups (get_pending_follow_ups) and
        RESOLVE them (resolve_follow_ups), but cannot create new ones.
        """
        if follow_ups:
            logger.debug(
                "Ego cycle %s produced %d follow-ups (creation disabled)",
                cycle_id, len(follow_ups),
            )
        return 0

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
        """Mark a follow-up as completed by the ego.

        Respects pinned status — pinned follow-ups cannot be cleared by ego.
        """
        existing = await follow_up_crud.get_by_id(self._db, follow_up_id)
        if existing and existing.get("pinned"):
            logger.info(
                "Follow-up %s is pinned — ego cannot clear", follow_up_id[:8],
            )
            return
        await follow_up_crud.update_status(
            self._db, follow_up_id, "completed",
            resolution_notes="Resolved by ego cycle",
        )
