"""Outreach pipeline — orchestrates governance → draft → format → deliver → track."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.content.drafter import ContentDrafter
from genesis.content.egress import gate
from genesis.content.formatter import ContentFormatter
from genesis.content.types import DraftRequest, FormatTarget, FormattedContent
from genesis.db.crud import autonomous_email_sends as aes
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import outreach as outreach_crud
from genesis.outreach.config import OutreachConfig
from genesis.outreach.fresh_eyes import FreshEyesReview
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.reply_waiter import DEFAULT_REPLY_TIMEOUT_S, ReplyWaiter
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


def _awaited_dup_key(request: OutreachRequest) -> str:
    """Identity of an awaited approval for the in-flight duplicate guard —
    the same (signal_type, topic) a duplicate prompt would collide on."""
    return f"{request.signal_type}\x00{request.topic}"


def _suppressed_awaited_result() -> OutreachResult:
    """Terminal result for a concurrent duplicate awaited approval that was
    suppressed (never sent) because an identical prompt is still pending."""
    return OutreachResult(
        outreach_id=str(uuid.uuid4()),
        status=OutreachStatus.REJECTED,
        channel="",
        message_content="",
        governance_result=None,
    )


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
        self._thread_tracker: object | None = None  # Set via set_thread_tracker()
        self._topic_manager = None
        self._forum_chat_id: str | None = None
        # WS-8 email autonomy gate — injected via set_autonomy_gate() during
        # autonomy init (runs after outreach init, so ApprovalManager exists).
        # None ⇒ no gating (e.g. pre-wiring / standalone tests).
        self._autonomy_gate: object | None = None
        # WS-8 PR-D — per-send owner notification for autonomous (GRANTED-cell)
        # email sends.  MUTED by default; the dashboard "Activity" ledger is the
        # primary visibility path.  Toggled via set_autonomous_send_notify().
        self._autonomous_send_notify: bool = False
        # In-flight awaited approvals, keyed by (signal_type, topic). While
        # one is pending, a concurrent duplicate awaited-approval is
        # SUPPRESSED rather than delivered as a second prompt: two identical
        # pending prompts in one chat make resolve_scoped_pending ambiguous
        # (len(eligible) != 1), so a plain APPROVE would resolve NEITHER. The
        # key is released when the wait ends (answer OR timeout), so a retry
        # after a timed-out approval is never blocked — the distinction the
        # stateless governance dedup window cannot make.
        self._inflight_awaited: set[str] = set()

    def set_reply_waiter(self, waiter: ReplyWaiter) -> None:
        """Attach a ReplyWaiter for bidirectional outreach."""
        self._reply_waiter = waiter

    def set_thread_tracker(self, tracker: object) -> None:
        """Attach a ThreadTracker for email thread auto-registration."""
        self._thread_tracker = tracker

    def set_autonomy_gate(self, gate: object) -> None:
        """Attach the WS-8 email autonomy gate (deterministic owner-authorization
        check applied to outbound email in ``_deliver``)."""
        self._autonomy_gate = gate

    def set_autonomous_send_notify(self, enabled: bool) -> None:
        """Toggle the per-send owner notification for autonomous email sends
        (WS-8 PR-D).  Muted by default; the dashboard Activity tab is primary."""
        self._autonomous_send_notify = bool(enabled)

    async def _record_autonomous_send(
        self,
        request: OutreachRequest,
        cell: tuple[str, str, str],
        recipient: str,
        outreach_id: str,
        sent_at: str,
    ) -> None:
        """Log an autonomous (GRANTED-cell) email send (WS-8 PR-D).  Best-effort:
        the email is already delivered, so a ledger/notify failure must NEVER
        unwind it.  Writes the owner-visible ledger row + bumps the cell's
        ``last_used_at`` (an autonomous send is not a competence signal — only the
        ABSENCE of corrections is — so it does NOT record a success)."""
        domain, verb, risk = cell
        try:
            await aes.create(
                self._db,
                id=str(uuid.uuid4()),
                recipient=recipient,
                subject=request.topic or "",
                thread_id=getattr(request, "thread_id", None),
                outreach_id=outreach_id,
                cell_domain=domain,
                cell_verb=verb,
                cell_risk_class=risk,
                sent_at=sent_at,
            )
            await cg.touch_used(
                self._db, domain=domain, verb=verb, risk_class=risk, used_at=sent_at,
            )
        except Exception:
            logger.warning("autonomous-send ledger write failed", exc_info=True)
        await self._maybe_notify_autonomous_send(request, recipient)

    async def _maybe_notify_autonomous_send(
        self, request: OutreachRequest, recipient: str,
    ) -> None:
        """Owner notification for an autonomous send — MUTED by default (WS-8
        PR-D); the dashboard Activity tab is the primary visibility path."""
        if not self._autonomous_send_notify:
            return
        adapter = self._channels.get("telegram")
        owner = self._recipients.get("telegram")
        if adapter is None or not owner:
            return
        subject = request.topic or "(no subject)"
        try:
            await adapter.send_message(
                owner,
                f'Autonomous email sent to {recipient} — "{subject}". '
                "Review or flag it in the dashboard Activity tab.",
            )
        except Exception:
            logger.warning("autonomous-send notification failed", exc_info=True)

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
                error=gov.reason,  # surface the reason so retry logs aren't blank
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

        if request.verbatim:
            # Machine-factual notification (e.g. task status): deliver the
            # context EXACTLY, with no LLM in the path, so it can never be
            # creatively rewritten or invent detail. Governance already ran
            # above; this only removes the drafter. Fall back to topic if a
            # caller ever leaves context empty — never deliver an empty string.
            formatted = self._formatter.format(
                request.context or request.topic, format_target,
            )
        else:
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
        Still applies dedup to prevent alert spam (e.g. sentinel approvals).
        ``reply_markup`` is forwarded to the adapter for inline keyboard buttons.
        """
        outreach_id = str(uuid.uuid4())

        # Lightweight dedup — only check, skip other governance.
        # Wrapped in try/except: submit_raw must stay reliable even if DB is down.
        try:
            if await self._governance.is_duplicate(request):
                logger.warning(
                    "submit_raw dedup suppressed: signal=%s topic=%r",
                    request.signal_type, request.topic,
                )
                return OutreachResult(
                    outreach_id=outreach_id,
                    status=OutreachStatus.REJECTED,
                    channel="",
                    message_content="",
                    governance_result=None,
                )
        except Exception:
            logger.debug("submit_raw dedup check failed, proceeding", exc_info=True)

        channel = request.channel or self._select_channel(request.category)
        format_target = _CHANNEL_FORMAT.get(channel, FormatTarget.GENERIC)
        formatted = self._formatter.format(text, format_target)
        return await self._deliver(outreach_id, channel, formatted, request, None,
                                   reply_markup=reply_markup)

    async def submit_urgent(self, request: OutreachRequest) -> OutreachResult:
        outreach_id = str(uuid.uuid4())
        channel = request.channel or self._select_channel(request.category)
        format_target = _CHANNEL_FORMAT.get(channel, FormatTarget.GENERIC)

        if request.verbatim:
            # Deliver `context` EXACTLY — no LLM in the path — same contract as
            # submit(). An urgent caller that opts into verbatim is relaying a
            # machine fact (e.g. process_reaper kill alerts) that must never be
            # creatively rewritten. Fall back to `topic` so an empty context
            # never delivers an empty string.
            formatted = self._formatter.format(
                request.context or request.topic, format_target,
            )
        else:
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
        timeout_s: float = DEFAULT_REPLY_TIMEOUT_S,
    ) -> tuple[OutreachResult, str | None]:
        """Submit outreach and wait for user reply. Returns (result, reply_text)."""
        if not self._reply_waiter:
            logger.warning("submit_and_wait called without reply_waiter — falling back to submit")
            result = await self.submit(request)
            return result, None

        key = _awaited_dup_key(request)
        if key in self._inflight_awaited:
            logger.warning(
                "submit_and_wait: concurrent duplicate awaited approval suppressed "
                "(signal=%s topic=%r already awaiting a reply)",
                request.signal_type,
                request.topic,
            )
            return _suppressed_awaited_result(), None
        self._inflight_awaited.add(key)
        try:
            result = await self.submit(request)
            if result.status != OutreachStatus.DELIVERED or not result.delivery_id:
                return result, None

            # Register BEFORE attaching context: set_context drops keys with no
            # registered waiter, so the old order (context first, registration
            # deferred to wait_for_reply) left every waiter on this path
            # contextless — standalone replies were structurally unresolvable
            # (observed live 2026-07-16; every reply degraded to
            # implicit_activity).
            self._reply_waiter.register(result.delivery_id)
            self._attach_waiter_context(result, result.delivery_id)
            reply = await self._reply_waiter.wait_for_reply(
                result.delivery_id, timeout_s=timeout_s,
            )
            return result, reply
        finally:
            self._inflight_awaited.discard(key)

    def _attach_waiter_context(self, result: OutreachResult, *keys: str) -> None:
        """Record the delivered chat+topic on waiter *keys* so standalone
        text in that same topic can resolve them (scoped, never cross-chat).
        """
        if not self._reply_waiter or not result.chat_id:
            return
        thread_key = f"{result.chat_id}:{result.thread_id if result.thread_id is not None else 'dm'}"
        for key in keys:
            try:
                if not self._reply_waiter.set_context(key, thread_key):
                    # Tripwire: a dropped context means an ordering bug —
                    # the waiter must be registered before context attach.
                    logger.warning(
                        "set_context dropped for unregistered waiter %s — "
                        "standalone replies will not resolve it", key,
                    )
            except Exception:
                logger.debug("set_context failed for %s", key, exc_info=True)

    async def submit_raw_and_wait(
        self,
        text: str,
        request: OutreachRequest,
        *,
        timeout_s: float = DEFAULT_REPLY_TIMEOUT_S,
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

        key = _awaited_dup_key(request)
        if key in self._inflight_awaited:
            logger.warning(
                "submit_raw_and_wait: concurrent duplicate awaited approval suppressed "
                "(signal=%s topic=%r already awaiting a reply)",
                request.signal_type,
                request.topic,
            )
            return _suppressed_awaited_result(), None
        self._inflight_awaited.add(key)
        try:
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
            elif not waiter_key:
                # No pre-registered button key: register the delivery_id NOW,
                # before attaching context — same ordering trap as
                # submit_and_wait (set_context silently drops unregistered
                # keys, leaving the waiter ineligible for standalone replies).
                self._reply_waiter.register(result.delivery_id)
            self._attach_waiter_context(result, actual_key, result.delivery_id)

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
        finally:
            self._inflight_awaited.discard(key)

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
        gate_cleared: bool = False,
    ) -> OutreachResult:
        adapter = self._channels.get(channel)
        recipient = request.validated_recipient or self._recipients.get(channel, "")

        # Email self-send / no-recipient terminal skip (WS-8 spam-loop fix). A
        # send to the agent's OWN address, or an email with no resolved
        # recipient, is never valid outreach — drop it terminally (IGNORED)
        # here, BEFORE the no-recipient defer below (which would retry-loop in
        # the deferred-work queue) and BEFORE the gate below (which would HELD
        # it and flood approvals). Fires regardless of gate_cleared: an approved
        # self-send must still never be sent.
        if channel == "email":
            self_addr = getattr(self._channels.get("email"), "from_address", None)
            if not recipient:
                logger.warning(
                    "Outreach %s: email has no resolved recipient — "
                    "skipping (IGNORED)", outreach_id,
                )
                return OutreachResult(
                    outreach_id=outreach_id, status=OutreachStatus.IGNORED,
                    channel=channel, message_content=formatted.text,
                    error="no recipient resolved",
                )
            if self_addr and recipient == self_addr:
                logger.warning(
                    "Outreach %s: email recipient is the agent's own address "
                    "(%s) — skipping self-send (IGNORED)", outreach_id, recipient,
                )
                return OutreachResult(
                    outreach_id=outreach_id, status=OutreachStatus.IGNORED,
                    channel=channel, message_content=formatted.text,
                    error="self-addressed email suppressed",
                )

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

        # WS-8 autonomy capability gate — deterministic owner-authorization for
        # outbound email, BELOW the LLM tool call (Tenet 0). This is the sole
        # unbypassable chokepoint: every send path converges on _deliver. A held
        # send is recorded for owner approval and later resumed below the gate by
        # the resolution watcher (gate_cleared=True). Non-email channels and the
        # resume path skip it.
        # WS-8 PR-D: capture whether this send is AUTONOMOUS (gate allowed a
        # GRANTED cell) so the post-send block can log it to the owner-visible
        # ledger.  Stays None for non-email, the gate_cleared resume path, no
        # gate, or a held send.
        gate_cell: tuple[str, str, str] | None = None
        if channel == "email" and not gate_cleared and self._autonomy_gate is not None:
            decision = await self._autonomy_gate.check(
                request=request, recipient=recipient, message_text=formatted.text,
            )
            if not decision.allow:
                logger.info(
                    "Outreach %s HELD by email autonomy gate (pending=%s)",
                    outreach_id, decision.pending_id,
                )
                return OutreachResult(
                    outreach_id=outreach_id,
                    status=OutreachStatus.HELD,
                    channel=channel,
                    message_content=formatted.text,
                )
            if decision.reason == "granted":
                gate_cell = decision.cell

        # Determine delivery routing: supergroup, dm, or both
        routing = "supergroup"  # default
        if self._config:
            routing = self._config.delivery_routing.get(
                request.category.value,
                self._config.delivery_routing.get("default", "supergroup"),
            )

        # Resolve forum topic + supergroup routing.
        #
        # Forum topics and the supergroup ``forum_chat_id`` are TELEGRAM-ONLY
        # concepts (``forum_chat_id``/``topic_manager`` are wired solely from
        # the Telegram startup paths). They must never touch email/discord/voice
        # delivery: doing so overwrote prospect email recipients with the
        # Telegram forum chat id (a large negative integer), which Gmail rejects
        # as an invalid RFC 5321 address — the deferred-queue poison + blocking
        # SMTP that starved the event loop on 2026-06-14. Gate ALL forum routing
        # on the Telegram channel; other channels deliver to their own recipient
        # with no thread_id.
        thread_id = None
        topic_cat: str | None = None
        delivery_recipient = recipient
        if channel == "telegram":
            if routing in ("supergroup", "both") and self._topic_manager is not None:
                topic_cat = self._topic_manager.resolve_outreach_category(
                    request.category.value,
                )
                thread_id = await self._topic_manager.get_or_create_persistent(topic_cat)

            # Primary delivery — supergroup if available, fallback to DM
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

        # Outbound egress gate — deterministic anti-slop scrub (+ PII scan for
        # external-delivery channels). Fires for external channels (email/
        # discord) and CONTENT review drafts; user-facing channels (telegram/
        # voice) are left untouched. The spaced-em-dash fix is applied to the
        # delivered text; non-fixable tells are logged. PII on an external send
        # quarantines (don't leak secrets to a third party).
        egress = gate(
            formatted.text, channel=channel, category=request.category.value,
        )
        if egress.applied:
            if egress.fixes_applied or egress.flags:
                logger.info(
                    "Egress gate [%s/%s]: fixes=%s flags=%s",
                    channel, request.category.value,
                    egress.fixes_applied, egress.flags,
                )
            if egress.quarantined:
                logger.warning(
                    "Outbound %s quarantined: %s patterns %s",
                    channel, egress.scan.risk_level, egress.scan.detected,
                )
                return OutreachResult(
                    outreach_id=outreach_id,
                    status=OutreachStatus.FAILED,
                    channel=channel,
                    message_content=formatted.text,
                    error=f"Content scan quarantine: {egress.scan.detected}",
                )
            if egress.fixes_applied:
                formatted = replace(formatted, text=egress.text)

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
            if not gate_cleared:
                # Gate-cleared (resume) sends are retried by the WS-8 email-gate
                # drain, NOT the deferred-work queue: deferring here would route
                # the retry back through _deliver and re-gate it. The drain owns
                # resume retries (it leaves the hold 'held' on failure).
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

            # WS5 Discord capability SHADOW-gate: observe (never hold) autonomous Discord
            # sends AFTER the post is already out, so the community-facing send is NEVER
            # delayed by the shadow write (any WAL contention only defers the internal
            # record). Records what a capability gate WOULD decide. Best-effort + read-
            # only; the gate_cleared resume path is below-the-gate → not observed.
            if channel == "discord" and not gate_cleared:
                # Import + call are wrapped so a shadow/import failure can never break the
                # already-completed send (uniform with the poll/reply doors).
                try:
                    from genesis.autonomy.shadow_gate import observe_discord_send

                    await observe_discord_send(
                        self._db, path="deliver", verb="send", risk_class="bulk",
                        target=str(delivery_recipient), content=formatted.text,
                    )
                except Exception:  # noqa: BLE001 — shadow is best-effort; never break the send
                    logger.debug("deliver capability shadow observe failed", exc_info=True)

            # WS-8 PR-D: log autonomous (GRANTED-cell) email sends for owner
            # visibility (Activity tab), the flag-as-bad correction, and the
            # per-cell rate-limit guard.  Owner-APPROVED holds resume via
            # deliver_approved (gate_cleared, gate_cell None) and are NOT logged.
            if gate_cell is not None:
                await self._record_autonomous_send(
                    request, gate_cell, recipient, outreach_id, now,
                )

        # Auto-register email threads for reply tracking
        if channel == "email" and self._thread_tracker is not None:
            try:
                await self._thread_tracker.register(
                    message_id=str(delivery_id),
                    recipient=delivery_recipient,
                    subject=request.topic or "",
                    context={"signal_type": request.signal_type or request.category.value,
                             "outreach_id": outreach_id},
                )
            except Exception:
                logger.warning("Email thread registration failed", exc_info=True)

        # Voice secondary delivery — non-blocking (must not delay primary path).
        # A request may carry a short, factual `voice_text` TL;DR for the ear
        # (no file paths / tokens / commands read aloud); when absent, speak the
        # full delivered text (unchanged for every existing caller).
        if channel != "voice" and self._should_voice(request):
            voice = self._channels.get("voice")
            if voice:
                from genesis.util.tasks import tracked_task
                spoken = request.voice_text or formatted.text
                tracked_task(
                    voice.send_message("", spoken),
                    name=f"voice-chime-{outreach_id[:8]}",
                )
                logger.info("Voice chime queued for %s", outreach_id)

        logger.info("Outreach %s delivered via %s (delivery_id=%s)", outreach_id, channel, delivery_id)
        return OutreachResult(
            outreach_id=outreach_id,
            status=OutreachStatus.DELIVERED,
            channel=channel,
            message_content=formatted.text,
            delivery_id=str(delivery_id),
            governance_result=gov,
            chat_id=str(delivery_recipient) if channel == "telegram" else None,
            thread_id=thread_id if channel == "telegram" else None,
        )

    async def deliver_approved(
        self, pending: dict, *, subject: str | None = None,
    ) -> OutreachResult:
        """Send a previously-held email BELOW the autonomy gate.

        Called ONLY by the resolution watcher after the owner approved the
        hold.  The body was drafted + formatted when held, so it is delivered
        verbatim (no re-draft, no governance).  ``gate_cleared=True`` skips
        re-gating — the flag is set only here (trusted code), never by the LLM,
        so it cannot be a re-entry / bypass vector.
        """
        req = OutreachRequest(
            category=OutreachCategory(pending["category"]),
            topic=subject or "",
            context=pending["message"],
            salience_score=0.5,
            signal_type="email_gate_resume",
            channel="email",
            validated_recipient=pending["validated_recipient"],
            thread_id=pending.get("thread_id"),
        )
        formatted = FormattedContent(text=pending["message"], target=FormatTarget.EMAIL)
        return await self._deliver(
            str(uuid.uuid4()), "email", formatted, req, None, gate_cleared=True,
        )

    def _should_voice(self, request: OutreachRequest) -> bool:
        """Check if this request qualifies for voice (spoken-aloud) delivery.

        Pure allowlist: ``config.voice_alert_ids`` IS the menu of what gets
        spoken. A request voices only if its ``signal_type`` or any part of
        its ``source_id`` matches an allowlist entry by prefix — the same
        matching convention as the immediate-escalation list in
        ``health_outreach.py``. There is no category-based fallback, so
        nothing is voiced by an invisible rule: every spoken alert is one
        editable line in ``voice_alert_ids`` (config.py / outreach.yaml).
        Everything still reaches Telegram regardless; this gate only
        controls what interrupts the user out loud.

        Matching notes:
        - ``source_id`` is comma-split to handle the batched health-alert
          envelope (``scheduler.py`` joins ids with commas).
        - prefix match (``startswith``) lets a family entry like
          ``provider:credit_exhaustion`` match ``…:<provider>``; keep
          entries specific to avoid unintended prefix hits.
        - ``signal_type`` matching is how non-health signals opt in
          (e.g. ``sentinel_escalation``; ``task_complete`` / ``task_alert`` are
          set by ``autonomy/executor/engine.py`` ``_notify`` for attention-worthy
          task notifications — routine ``task_progress`` is deliberately absent).
        """
        if not self._channels.get("voice"):
            return False
        if not self._config:
            return False
        if not self._in_voice_hours():
            return False
        # Pre-strip allowlist entries so a stray space in hand-edited
        # outreach.yaml doesn't silently break a match.
        allow = [aid.strip() for aid in self._config.voice_alert_ids if aid.strip()]
        candidates = [request.signal_type or "", *(request.source_id or "").split(",")]
        return any(
            cand.strip().startswith(aid)
            for cand in candidates
            if cand.strip()
            for aid in allow
        )

    def _in_voice_hours(self) -> bool:
        """Check if current time is within voice notification hours."""
        if not self._config:
            return False
        try:
            import zoneinfo

            from genesis.env import user_timezone
            tz = zoneinfo.ZoneInfo(user_timezone())
            now = datetime.now(tz)
            start, end = self._config.voice_hours
            if start < end:
                return start <= now.hour < end
            # Wraps midnight (e.g., 9am–2am)
            return now.hour >= start or now.hour < end
        except Exception:
            logger.debug("Voice hours check failed", exc_info=True)
            return False

    async def _defer(
        self, outreach_id: str, channel: str, content: str,
        request: OutreachRequest, reason: str,
    ) -> None:
        if not self._deferred_queue:
            return
        try:
            # "outreach_fallback" — deferred-queue work tag (not in model_routing.yaml).
            # No own routing chain; used for cost/event tracking only.
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
                staleness_ttl_s=14400,  # 4h — accommodates 5 retries with backoff
            )
        except Exception:
            logger.exception("Failed to defer outreach %s", outreach_id)
