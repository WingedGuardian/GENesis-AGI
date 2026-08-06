"""Engagement tracking — timeout detection and reply recording."""

from __future__ import annotations

import logging
from email.utils import parseaddr

import aiosqlite

from genesis.db.crud import outreach as outreach_crud

logger = logging.getLogger(__name__)


def make_reply_engagement_bridge(tracker: EngagementTracker):
    """Build the mail→outreach reply bridge for ``ReplyPoller.set_engagement_bridge``.

    The poller calls it with the matched ``thread`` dict (whose ``context`` was
    stashed at send time by ``OutreachPipeline._deliver`` thread registration)
    and the parsed reply. Resolves ``context["outreach_id"]`` and records the
    engagement NULL-guarded. Every miss degrades to a logged no-op — reply
    tracking must never depend on this bridge. Injected from
    ``runtime/init/outreach.py`` so ``genesis.mail`` stays outreach-agnostic.
    """

    async def _bridge(thread: dict, reply) -> None:
        context = thread.get("context")
        if not isinstance(context, dict):
            logger.debug(
                "Reply thread %s has no context dict — engagement bridge skipped",
                thread.get("id"),
            )
            return
        outreach_id = context.get("outreach_id")
        if not outreach_id:
            # Pre-bridge sends (e.g. historical foreground sends) have no
            # outreach_id — genuine engagement, but no ledger row to update.
            logger.debug(
                "Reply thread %s context lacks outreach_id — engagement bridge skipped",
                thread.get("id"),
            )
            return
        # Only a genuine, human reply from the address we actually mailed counts
        # as engagement. match_reply accepts any message carrying our Message-ID,
        # so gate out automated mail (OOO/bounce/list) and foreign/spoofed senders
        # before they inflate the reply rate / resolve ledger predictions.
        if getattr(reply, "is_auto", False):
            logger.debug(
                "Reply on thread %s is automated (OOO/bounce/list) — not counted as engagement",
                thread.get("id"),
            )
            return
        recipient = (thread.get("recipient") or "").strip().lower()
        sender_addr = parseaddr(getattr(reply, "sender", "") or "")[1].strip().lower()
        if recipient and sender_addr and sender_addr != recipient:
            logger.debug(
                "Reply on thread %s from %s != recipient %s — not counted as engagement",
                thread.get("id"), sender_addr, recipient,
            )
            return
        reply_text = getattr(reply, "body_preview", "") or ""
        await tracker.record_reply_if_pending(str(outreach_id), reply_text)

    return _bridge


class EngagementTracker:
    """Tracks user engagement with outreach messages."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def check_timeouts(self, timeout_hours: int = 24) -> int:
        cursor = await self._db.execute(
            "SELECT id FROM outreach_history "
            "WHERE delivered_at IS NOT NULL "
            "AND engagement_outcome IS NULL "
            "AND delivered_at < datetime('now', ? || ' hours')",
            (f"-{timeout_hours}",),
        )
        rows = await cursor.fetchall()
        count = 0
        for row in rows:
            outreach_id = row[0] if isinstance(row, tuple) else row["id"]
            await outreach_crud.record_engagement(
                self._db, outreach_id, engagement_outcome="ignored", engagement_signal="timeout",
            )
            count += 1
        if count:
            logger.info("Marked %d outreach items as ignored (timeout)", count)
        return count

    async def find_outreach_for_reply(self, delivery_id: str) -> str | None:
        row = await outreach_crud.find_by_delivery_id(self._db, delivery_id)
        if row and row.get("engagement_outcome") is None:
            return row["id"]
        return None

    async def record_reply(self, outreach_id: str, reply_text: str) -> bool:
        """Record that the user replied to an outreach message."""
        try:
            await self._db.execute(
                "UPDATE outreach_history SET user_response = ?, "
                "engagement_outcome = 'useful', engagement_signal = 'user_reply' "
                "WHERE id = ?",
                (reply_text[:2000], outreach_id),
            )
            await self._db.commit()
            logger.info("Recorded reply engagement for outreach %s", outreach_id)
            return True
        except Exception:
            logger.warning("Failed to record reply engagement for %s", outreach_id, exc_info=True)
            return False

    async def record_reply_if_pending(self, outreach_id: str, reply_text: str) -> bool:
        """Guarded reply recording: promote NULL or a MECHANICAL marker only.

        Unlike ``record_reply`` (unconditional — Telegram/dashboard callers
        depend on its upgrade semantics), this never clobbers a human-set
        outcome: a 2nd reply, or a row marked 'acted_on'/'engaged' by the
        user, is a clean no-op. Machine-stamped states ARE overridable — the
        24h timeout ('ignored'/'timeout'), implicit-activity, and auto-digest
        markers are defaults a late real reply must beat (the reply poller
        runs 4-hourly, so 24-72h replies routinely arrive post-timeout).

        "Machine-stamped" is judged on the outcome+signal PAIRING, not the
        signal alone: the dashboard /engage route rewrites the outcome but
        leaves the old signal in place, so a human 'not_useful' can sit on a
        stale 'timeout' signal — the mechanical writers only ever pair
        ignored/ambivalent with a mechanical signal, so any other outcome is
        definitionally a human verdict and stays protected.
        Used by the mail reply→engagement bridge.
        """
        from genesis.outreach.types import MECHANICAL_ENGAGEMENT_SIGNALS_SQL_IN

        try:
            # The IN-lists are trusted module constants (sorted literals), not
            # user input — same rationale as POSITIVE_ENGAGEMENT_SQL_IN.
            cursor = await self._db.execute(
                "UPDATE outreach_history SET user_response = ?, "  # noqa: S608
                "engagement_outcome = 'useful', engagement_signal = 'user_reply' "
                "WHERE id = ? AND (engagement_outcome IS NULL "
                "OR (engagement_outcome IN ('ignored', 'ambivalent') "
                f"AND engagement_signal IN ({MECHANICAL_ENGAGEMENT_SIGNALS_SQL_IN})))",
                (reply_text[:2000], outreach_id),
            )
            await self._db.commit()
            if cursor.rowcount > 0:
                logger.info("Recorded reply engagement for outreach %s", outreach_id)
                return True
            logger.debug(
                "Outreach %s already has an engagement outcome — reply not re-recorded",
                outreach_id,
            )
            return False
        except Exception:
            logger.warning(
                "Failed to record reply engagement for %s", outreach_id, exc_info=True,
            )
            return False

    async def record_implicit_engagement(self, outreach_id: str) -> bool:
        """Record that the user was active after receiving outreach (weak signal).

        Only upgrades NULL → ambivalent. Never downgrades engaged → ambivalent.
        """
        try:
            cursor = await self._db.execute(
                "UPDATE outreach_history SET engagement_outcome = 'ambivalent', "
                "engagement_signal = 'implicit_activity' "
                "WHERE id = ? AND engagement_outcome IS NULL",
                (outreach_id,),
            )
            await self._db.commit()
            if cursor.rowcount > 0:
                logger.debug("Recorded implicit engagement for outreach %s", outreach_id)
                return True
            return False
        except Exception:
            logger.debug("Failed to record implicit engagement for %s", outreach_id, exc_info=True)
            return False
