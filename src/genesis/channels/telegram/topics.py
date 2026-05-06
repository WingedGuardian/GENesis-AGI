"""TopicManager — persistent category-based forum topics in Telegram supergroups.

Manages forum topics grouped by category (Conversation, Alerts, Morning Reports,
per-depth Reflections, Surplus, Recon) rather than per-session. Topics are
created once and reused forever, stored in the ``telegram_topics`` DB table
for persistence across restarts.

Requires: bot must be admin in a supergroup with topics enabled.
Graceful degradation: if not configured or permissions insufficient,
all topic operations are silent no-ops.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.error import BadRequest

if TYPE_CHECKING:
    import aiosqlite
    from telegram import Bot

logger = logging.getLogger(__name__)

# Default category → display name mapping
DEFAULT_CATEGORIES: dict[str, str] = {
    "conversation": "Conversation",
    "morning_report": "Morning Reports",
    "alert": "Alerts",
    "reflection_micro": "Micro Reflections",
    "reflection_light": "Light Reflections",
    "reflection_deep": "Deep Reflections",
    "reflection_strategic": "Strategic Reflections",
    "surplus": "Surplus",
    "recon": "Recon",
    "ego_proposals": "Ego Proposals",
    # Autonomous CLI approval prompts — inline ✅ buttons + optional
    # "approve all N pending" batch button.  Bare "approve"/"reject"
    # text messages in this topic resolve the most recent pending request.
    "approvals": "Approvals",
    # Content pipeline drafts awaiting user review before external publishing.
    "content_review": "Content Review",
}


class TopicManager:
    """Manages persistent category-based forum topics in a Telegram supergroup."""

    def __init__(
        self,
        bot: Bot,
        forum_chat_id: int,
        *,
        db: aiosqlite.Connection | None = None,
        categories: dict[str, str] | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = forum_chat_id
        self._db = db
        self._categories = categories or DEFAULT_CATEGORIES
        self._persistent_topics: dict[str, int] = {}  # category → thread_id
        self._create_lock = asyncio.Lock()

    async def load_persisted(self) -> None:
        """Load persisted topic mappings from DB. Call once after construction."""
        if self._db is None:
            return
        try:
            async with self._db.execute(
                "SELECT category, thread_id FROM telegram_topics WHERE chat_id = ?",
                (self._chat_id,),
            ) as cur:
                rows = await cur.fetchall()
                for row in rows:
                    self._persistent_topics[row[0]] = row[1]
            if self._persistent_topics:
                logger.info(
                    "Loaded %d persisted topic mappings",
                    len(self._persistent_topics),
                )
        except Exception:
            logger.warning("Failed to load persisted topics (table may not exist)", exc_info=True)

    async def _persist_topic(self, category: str, thread_id: int) -> None:
        """Save a category → thread_id mapping to DB."""
        if self._db is None:
            return
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO telegram_topics (category, thread_id, chat_id, created_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (category, thread_id, self._chat_id),
            )
            await self._db.commit()
        except Exception:
            logger.warning("Failed to persist topic mapping for %s", category, exc_info=True)

    async def get_or_create_persistent(
        self, category: str, display_name: str | None = None,
    ) -> int | None:
        """Get existing persistent topic or create a new one. Returns thread_id or None."""
        if category in self._persistent_topics:
            return self._persistent_topics[category]

        async with self._create_lock:
            # Re-check after acquiring lock (another coroutine may have created it)
            if category in self._persistent_topics:
                return self._persistent_topics[category]

            name = (display_name or self._categories.get(category, category))[:128]
            try:
                topic = await self._bot.create_forum_topic(
                    chat_id=self._chat_id, name=name,
                )
                thread_id = topic.message_thread_id
                self._persistent_topics[category] = thread_id
                await self._persist_topic(category, thread_id)
                logger.info(
                    "Created persistent topic '%s' (thread_id=%d) for category '%s'",
                    name, thread_id, category,
                )
                return thread_id
            except Exception:
                # Log every failure at ERROR, not warn-once at WARNING.
                # The caller retries lazy-creation on every delivery, so a
                # persistent failure (rate limit, perms regression, Telegram
                # API flake) used to be invisible after the first hit and
                # silently routed messages to DM fallback.  Logging every
                # attempt makes the problem visible in health_errors MCP and
                # the dashboard error feed immediately.
                logger.error(
                    "Failed to create forum topic for category '%s' "
                    "(chat_id=%d, name=%r); caller will fall back to DM "
                    "delivery",
                    category, self._chat_id, name, exc_info=True,
                )
                return None

    def get_thread_id(self, category: str) -> int | None:
        """Get the thread_id for a category without creating. Returns None if not created yet."""
        return self._persistent_topics.get(category)

    async def send_to_category(
        self,
        category: str,
        text: str,
        *,
        parse_mode: str | None = "HTML",
    ) -> str | None:
        """Send a message to a category's persistent topic. Returns message_id or None.

        Splits long messages to stay within Telegram's 4096-char limit.
        Returns the message_id of the first chunk (used for reply mapping).
        """
        from genesis.channels.telegram._handler_helpers import _split_for_telegram

        thread_id = await self.get_or_create_persistent(category)
        if thread_id is None:
            return None

        chunks = _split_for_telegram(text)
        first_msg_id: str | None = None

        for chunk in chunks:
            msg_id = await self._send_single(
                chunk, thread_id=thread_id, category=category,
                parse_mode=parse_mode,
            )
            if msg_id is None:
                # If the first chunk fails, abort entirely.
                # If a later chunk fails, return what we have.
                if first_msg_id is None:
                    return None
                logger.warning(
                    "Partial delivery to '%s': %d/%d chunks sent",
                    category, chunks.index(chunk), len(chunks),
                )
                break
            if first_msg_id is None:
                first_msg_id = msg_id

        return first_msg_id

    async def _send_single(
        self,
        text: str,
        *,
        thread_id: int,
        category: str,
        parse_mode: str | None = "HTML",
    ) -> str | None:
        """Send a single message chunk to a topic. Handles retries for
        deleted topics and parse failures. Returns message_id or None."""
        try:
            kwargs: dict = {
                "chat_id": self._chat_id,
                "text": text,
                "message_thread_id": thread_id,
            }
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            try:
                msg = await self._bot.send_message(**kwargs)
            except BadRequest as exc:
                err_msg = str(exc).lower()
                if "thread not found" in err_msg:
                    # Topic was deleted — recreate it
                    logger.warning("Topic for '%s' was deleted, recreating", category)
                    self._persistent_topics.pop(category, None)
                    thread_id = await self.get_or_create_persistent(category)
                    if thread_id is None:
                        return None
                    kwargs["message_thread_id"] = thread_id
                    msg = await self._bot.send_message(**kwargs)
                elif parse_mode and ("can't parse" in err_msg or "parse entities" in err_msg):
                    logger.warning(
                        "Topic send with %s failed, retrying plain",
                        parse_mode, exc_info=True,
                    )
                    kwargs.pop("parse_mode", None)
                    msg = await self._bot.send_message(**kwargs)
                else:
                    raise
            return str(msg.message_id)
        except Exception:
            logger.error(
                "Failed to send to topic category '%s'",
                category, exc_info=True,
            )
            return None

    async def close_orphaned_topics(self, thread_ids: set[int]) -> int:
        """Close orphaned forum topics by thread_id. Returns count closed."""
        closed = 0
        for tid in sorted(thread_ids):
            try:
                await self._bot.close_forum_topic(
                    chat_id=self._chat_id, message_thread_id=tid,
                )
                closed += 1
                logger.info("Closed orphaned topic thread_id=%d", tid)
            except BadRequest:
                logger.debug("Topic thread_id=%d already closed or deleted", tid)
            except Exception:
                logger.warning(
                    "Failed to close orphaned topic thread_id=%d", tid, exc_info=True,
                )
        return closed

    def resolve_outreach_category(self, outreach_category: str) -> str:
        """Map an outreach category to a topic category.

        E.g., "blocker" → "alert", "surplus" → "surplus".
        """
        mapping = {
            "blocker": "alert",
            "alert": "alert",
            "morning_report": "morning_report",
            "digest": "morning_report",
            "surplus": "surplus",
            "recon": "recon",
            "ego_proposals": "ego_proposals",
            # Autonomous CLI approvals get their own topic so the inline
            # button UX and "approve all N pending" semantics are clearly
            # scoped — they don't mix with general alerts.
            "approval": "approvals",
            "content": "content_review",
        }
        return mapping.get(outreach_category, "surplus")
