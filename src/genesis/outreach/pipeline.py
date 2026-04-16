"""Outreach pipeline — orchestrates governance → draft → format → deliver → track."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.content.drafter import ContentDrafter
from genesis.content.formatter import ContentFormatter
from genesis.content.types import DraftRequest, FormatTarget, FormattedContent
from genesis.db.crud import outreach as outreach_crud
from genesis.outreach.config import OutreachConfig
from genesis.outreach.fresh_eyes import FreshEyesReview
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.reply_waiter import ReplyWaiter
from genesis.outreach.types import (
    GovernanceVerdict,
    OutreachCategory,
    OutreachRequest,
    OutreachResult,
    OutreachStatus,
)

logger = logging.getLogger(__name__)

_ALERT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "identity" / "OUTREACH_ALERT.md"
_URGENT_CATEGORIES = frozenset({OutreachCategory.BLOCKER, OutreachCategory.ALERT})

_CHANNEL_FORMAT = {
    "telegram": FormatTarget.TELEGRAM,
    "email": FormatTarget.EMAIL,
}


class OutreachPipeline:
    """Main outreach orchestrator."""

    def __init__(
        self,
        governance: GovernanceGate,
        drafter: ContentDrafter,
        formatter: ContentFormatter,
        channels: dict[str, object],
        *,
        fresh_eyes: FreshEyesReview | None = None,
        deferred_queue: object | None = None,
        db: aiosqlite.Connection | None = None,
        config: OutreachConfig | None = None,
        recipients: dict[str, str] | None = None,
        # GROUNDWORK(outreach-voice): Voice delivery for outreach notifications.
        # When voice_helper is available and adapter supports voice, outreach
        # could deliver voice notifications for high-priority items.
        voice_helper: object | None = None,
    ) -> None:
        self._governance = governance
        self._drafter = drafter
        self._formatter = formatter
        self._channels = channels
        self._fresh_eyes = fresh_eyes
        self._deferred_queue = deferred_queue
        self._db = db
        self._config = config
        self._recipients = recipients or {}
        self._voice_helper = voice_helper  # GROUNDWORK(outreach-voice)
        self._reply_waiter: ReplyWaiter | None = None
        self._topic_manager = None
        self._forum_chat_id: str | None = None

    def set_reply_waiter(self, waiter: ReplyWaiter) -> None:
        """Attach a ReplyWaiter for bidirectional outreach."""
        self._reply_waiter = waiter

    def reload_config(self, config: OutreachConfig) -> None:
        """Hot-reload outreach config on pipeline and governance gate."""
        self._config = config
        self._governance._config = config

    def set_topic_manager(self, topic_manager) -> None:
        """Attach a TopicManager for routing outreach to forum topics."""
        self._topic_manager = topic_manager

    @property
    def topic_manager(self):
        """Public accessor for the attached TopicManager, or None.

        Exposed so handlers (e.g. the Telegram bare-text approval
        resolver) can look up topic thread_ids via a stable public
        API instead of reaching into the private ``_topic_manager``
        attribute.
        """
        return self._topic_manager

    def set_forum_chat_id(self, chat_id: int) -> None:
        """Set the supergroup chat ID for forum topic delivery."""
        self._forum_chat_id = str(chat_id)

    async def submit(self, request: OutreachRequest) -> OutreachResult:
        outreach_id = str(uuid.uuid4())

        gov = await self._governance.check(request)
        if gov.verdict == GovernanceVerdict.DENY:
            logger.info("Outreach denied: %s", gov.reason)
            return OutreachResult(
                outreach_id=outreach_id,
                status=OutreachStatus.REJECTED,
                channel="",
                message_content="",
                governance_result=gov,
            )

        if request.category == OutreachCategory.SURPLUS and self._fresh_eyes:
            review = await self._fresh_eyes.review(request.context, request.topic)
            if not review.approved:
                logger.info("Fresh-eyes rejected surplus: %s", review.reason)
                return OutreachResult(
                    outreach_id=outreach_id,
                    status=OutreachStatus.REJECTED,
                    channel="",
                    message_content="",
                    governance_result=gov,
                    error=f"Fresh-eyes rejected (score {review.score}): {review.reason}",
                )

        channel = request.channel or self._select_channel(request.category)
        format_target = _CHANNEL_FORMAT.get(channel, FormatTarget.GENERIC)

        is_urgent = request.category in _URGENT_CATEGORIES
        draft = await self._drafter.draft(DraftRequest(
            topic=request.topic,
            context=request.context,
            target=format_target,
            tone="urgent" if is_urgent else "conversational",
            max_length=None,
            system_prompt=self._load_alert_prompt() if is_urgent else None,
        ))

        formatted = self._formatter.format(draft.content.text, format_target)

        return await self._deliver(outreach_id, channel, formatted, request, gov)

    async def submit_raw(
        self, text: str, request: OutreachRequest,
        *, reply_markup: object | None = None,
    ) -> OutreachResult:
        """Deliver pre-formatted text. Skips governance and LLM drafter.

        For urgent infrastructure alerts where speed matters more than prose.
        ``reply_markup`` is forwarded to the adapter for inline keyboard buttons.
        """
        outreach_id = str(uuid.uuid4())
        channel = request.channel or self._select_channel(request.category)
        format_target = _CHANNEL_FORMAT.get(channel, FormatTarget.GENERIC)
        formatted = self._formatter.format(text, format_target)
        return await self._deliver(outreach_id, channel, formatted, request, None,
                                   reply_markup=reply_markup)

    async def submit_urgent(self, request: OutreachRequest) -> OutreachResult:
        outreach_id = str(uuid.uuid4())
        channel = request.channel or self._select_channel(request.category)
        format_target = _CHANNEL_FORMAT.get(channel, FormatTarget.GENERIC)

        draft = await self._drafter.draft(DraftRequest(
            topic=request.topic,
            context=request.context,
            target=format_target,
            tone="urgent",
            max_length=None,
            system_prompt=self._load_alert_prompt(),
        ))
        formatted = self._formatter.format(draft.content.text, format_target)
        return await self._deliver(outreach_id, channel, formatted, request, None)

    async def submit_and_wait(
        self,
        request: OutreachRequest,
        *,
        timeout_s: float = 300.0,
    ) -> tuple[OutreachResult, str | None]:
        """Submit outreach and wait for user reply. Returns (result, reply_text)."""
        if not self._reply_waiter:
            logger.warning("submit_and_wait called without reply_waiter — falling back to submit")
            result = await self.submit(request)
            return result, None

        result = await self.submit(request)
        if result.status != OutreachStatus.DELIVERED or not result.delivery_id:
            return result, None

        reply = await self._reply_waiter.wait_for_reply(
            result.delivery_id, timeout_s=timeout_s,
        )
        return result, reply

    async def submit_raw_and_wait(
        self,
        text: str,
        request: OutreachRequest,
        *,
        timeout_s: float = 300.0,
        reply_markup: object | None = None,
        waiter_key: str | None = None,
    ) -> tuple[OutreachResult, str | None]:
        """Submit pre-formatted text and wait for user reply.

        Skips governance and LLM drafting. Supports inline keyboard buttons:
        pass a pre-generated ``waiter_key`` (used in button callback_data) and
        ``reply_markup`` (the InlineKeyboardMarkup). After send, delivery_id is
        aliased to waiter_key so quote-reply also resolves the waiter.
        """
        if not self._reply_waiter:
            logger.warning("submit_raw_and_wait called without reply_waiter")
            result = await self.submit_raw(text, request, reply_markup=reply_markup)
            return result, None

        # Pre-register waiter if button key provided
        if waiter_key:
            self._reply_waiter.register(waiter_key)

        result = await self.submit_raw(text, request, reply_markup=reply_markup)
        if result.status != OutreachStatus.DELIVERED or not result.delivery_id:
            if waiter_key:
                self._reply_waiter.cancel(waiter_key)
            return result, None

        # Alias so quote-reply (by Telegram message_id) also resolves the waiter
        actual_key = waiter_key or result.delivery_id
        if waiter_key and result.delivery_id != waiter_key:
            self._reply_waiter.add_alias(result.delivery_id, waiter_key)

        try:
            reply = await self._reply_waiter.wait_for_reply(
                actual_key, timeout_s=timeout_s,
            )
        finally:
            # Clean up both keys to prevent stale entries (resolve only
            # pops one key; the counterpart would leak)
            if waiter_key and result.delivery_id != waiter_key:
                self._reply_waiter.remove(result.delivery_id, waiter_key)

        return result, reply

    @staticmethod
    def _load_alert_prompt() -> str | None:
        """Load the alert drafting system prompt from OUTREACH_ALERT.md."""
        try:
            return _ALERT_PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("OUTREACH_ALERT.md not found at %s", _ALERT_PROMPT_PATH)
            return None
        except OSError:
            logger.warning("Failed to read OUTREACH_ALERT.md", exc_info=True)
            return None

    def _select_channel(self, category: OutreachCategory) -> str:
        if self._config:
            prefs = self._config.channel_preferences
            return prefs.get(category.value, prefs.get("default", "telegram"))
        return "telegram"

    async def _deliver(
        self,
        outreach_id: str,
        channel: str,
        formatted: FormattedContent,
        request: OutreachRequest,
        gov: object | None,
        *,
        reply_markup: object | None = None,
    ) -> OutreachResult:
        adapter = self._channels.get(channel)
        recipient = self._recipients.get(channel, "")
        if not adapter or not recipient:
            logger.warning("No adapter/recipient for channel %s — deferring", channel)
            await self._defer(
                outreach_id, channel, formatted.text, request,
                f"No adapter or recipient for {channel}",
            )
            return OutreachResult(
                outreach_id=outreach_id,
                status=OutreachStatus.FAILED,
                channel=channel,
                message_content=formatted.text,
                error=f"No adapter or recipient for {channel}",
            )

        # Determine delivery routing: supergroup, dm, or both
        routing = "supergroup"  # default
        if self._config:
            routing = self._config.delivery_routing.get(
                request.category.value,
                self._config.delivery_routing.get("default", "supergroup"),
            )

        # Resolve forum topic for this outreach category
        thread_id = None
        topic_cat: str | None = None
        if routing in ("supergroup", "both") and self._topic_manager is not None:
            topic_cat = self._topic_manager.resolve_outreach_category(
                request.category.value,
            )
            thread_id = await self._topic_manager.get_or_create_persistent(topic_cat)

        # Primary delivery — supergroup if available, fallback to DM
        delivery_recipient = recipient
        if routing in ("supergroup", "both") and thread_id is not None and self._forum_chat_id:
            delivery_recipient = self._forum_chat_id
        else:
            # DM delivery — never pass thread_id to non-forum chats.
            # If we WANTED the topic but couldn't resolve a thread_id,
            # surface the fallback so operators know messages are landing
            # in DM instead of silently disappearing from the forum topic.
            # (This is the "where did my approval go?" signal that was
            # missing on 2026-04-10 when 7 approvals routed to DM.)
            if (
                routing in ("supergroup", "both")
                and self._forum_chat_id
                and self._topic_manager is not None
            ):
                logger.info(
                    "outreach category=%s wanted supergroup topic "
                    "routing but thread_id is None — falling back to DM "
                    "(target category=%s); check earlier ERROR logs from "
                    "TopicManager for the underlying cause",
                    request.category.value, topic_cat or "?",
                )
            thread_id = None

        try:
            delivery_id = await adapter.send_message(
                delivery_recipient, formatted.text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
            # Secondary DM copy for "both" mode (no buttons — primary only)
            if (routing == "both" and delivery_recipient != recipient
                    and self._forum_chat_id and thread_id is not None):
                try:
                    await adapter.send_message(recipient, formatted.text)
                except Exception:
                    logger.warning("DM copy failed for 'both' routing", exc_info=True)
        except Exception as exc:
            logger.error("Delivery failed on %s: %s", channel, exc, exc_info=True)
            await self._defer(outreach_id, channel, formatted.text, request, str(exc))
            return OutreachResult(
                outreach_id=outreach_id,
                status=OutreachStatus.FAILED,
                channel=channel,
                message_content=formatted.text,
                error=str(exc),
            )

        now = datetime.now(UTC).isoformat()
        if self._db:
            from genesis.outreach.governance import content_hash

            await outreach_crud.create(
                self._db,
                id=outreach_id,
                signal_type=request.signal_type or request.category.value,
                topic=request.topic,
                category=request.category.value,
                salience_score=request.salience_score,
                channel=channel,
                message_content=formatted.text,
                created_at=now,
                drive_alignment=request.drive_alignment,
                labeled_surplus=1 if request.labeled_surplus else 0,
                delivery_id=str(delivery_id),
                content_hash=content_hash(request.context),
            )
            await outreach_crud.record_delivery(self._db, outreach_id, delivered_at=now)

        logger.info("Outreach %s delivered via %s (delivery_id=%s)", outreach_id, channel, delivery_id)
        return OutreachResult(
            outreach_id=outreach_id,
            status=OutreachStatus.DELIVERED,
            channel=channel,
            message_content=formatted.text,
            delivery_id=str(delivery_id),
            governance_result=gov,
        )

    async def _defer(
        self, outreach_id: str, channel: str, content: str,
        request: OutreachRequest, reason: str,
    ) -> None:
        if not self._deferred_queue:
            return
        try:
            await self._deferred_queue.enqueue(
                work_type="outreach_delivery",
                call_site_id="outreach_fallback",
                priority=20,
                payload=json.dumps({
                    "outreach_id": outreach_id,
                    "channel": channel,
                    "content": content,
                    "category": request.category.value,
                    "topic": request.topic,
                }),
                reason=reason,
                staleness_policy="drain",
                staleness_ttl_s=3600,
            )
        except Exception:
            logger.exception("Failed to defer outreach %s", outreach_id)
