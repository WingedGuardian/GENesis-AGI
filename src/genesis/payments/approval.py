"""Owner approval of a card release: one Telegram button tap, held in server memory.

``PaymentApprovalRegistry.request(fields)`` builds the message text in code,
sends it to the owner's private chat with ``parse_mode=None`` and an Approve
button carrying ``payapprove:<nonce>``, and waits for one of three outcomes:
``granted``, ``refused`` or ``expired`` (no tap within ``DEFAULT_TIMEOUT_S``).

The pending request records the chat id, message id and text of the Message
Telegram RETURNED for the send, so a tap counts only when it comes from that
exact, unedited message. ``handle_tap`` grants only when every check holds:
the tapper is the owner (the first id of ``TELEGRAM_ALLOWED_USERS``, in
written order), the nonce is pending, the message is accessible, its chat id,
message id and text equal the record, and ``edit_date`` is unset. Any other
tap on a pending nonce refuses it; a nonce answers one tap only. Nothing is
persisted: a restart forgets every nonce, so an old button refuses.

This path never uses the outreach pipeline (it rewrites text and picks the
chat), ``AutonomousCliApprovalGate._send_request`` or ``ReplyWaiter``, and
writes no ``approval_requests`` row. ``PAYMENT_APPROVAL_ACTION_TYPE`` is still
registered in the gate's approve-all denylist, ``refuse_generic`` and both
dashboard approval filters, so a row of this type could never be resolved
generically if one were ever written.

Every request, grant, refusal and expiry is written to ``payment_vault_events``
through the vault service's own connection (``attach_audit``). It fails
closed: with no audit, or no bot attached (Telegram off or failed), every
request is refused, and a grant whose audit row cannot be written is refused.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PAYMENT_APPROVAL_ACTION_TYPE = "payment_card_release"
CALLBACK_PREFIX = "payapprove:"
DEFAULT_TIMEOUT_S = 10 * 60
# sendMessage text is "1-4096 characters after entities parsing"; Telegram
# counts UTF-16 code units, so a text over this is refused, never split.
_TG_MAX_UTF16 = 4096

GRANTED, REFUSED, EXPIRED = "granted", "refused", "expired"

Audit = Callable[[str, dict | None], Awaitable[bool]]


@dataclass
class _Pending:
    future: asyncio.Future
    chat_id: int | None = None  # None until the send returns
    message_id: int | None = None
    text: str | None = None
    detail: dict = field(default_factory=dict)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_text(fields: Mapping[str, str]) -> str:
    """The approval message. Raises ValueError on a field it will not show."""
    from genesis.security.card_shapes import contains_card_number

    if not fields:
        raise ValueError("no fields")
    lines = ["Payment approval"]
    for key, value in fields.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or not value:
            raise ValueError("fields must be non-empty strings")
        # One line per field: a newline or other control character in a value
        # could draw a line that reads as part of this message's own text.
        if not (key + value).isprintable():
            raise ValueError("a field holds a control character")
        lines.append(f"{key}: {value}")
    lines.append("Tap Approve to release the card for this payment, once.")
    text = "\n".join(lines)
    if contains_card_number(text):
        raise ValueError("a field holds a card number")
    if _utf16_len(text) > _TG_MAX_UTF16:
        raise ValueError("the message is over Telegram's 4096-character limit")
    return text


class PaymentApprovalRegistry:
    """Pending card-release approvals, in memory, matched to the message sent."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._bot = None
        self._owner: int | None = None
        self._audit: Audit | None = None
        self._pending: dict[str, _Pending] = {}

    def attach_audit(self, audit: Audit) -> None:
        self._audit = audit

    def attach_bot(self, bot, owner_id: int) -> None:
        self._bot, self._owner = bot, owner_id

    async def _record(self, event: str, detail: dict) -> bool:
        if self._audit is None:
            return False
        try:
            return bool(await self._audit(event, detail))
        except Exception as exc:  # noqa: BLE001 - a lost row is a refusal, never a crash
            logger.error("payment approval audit failed (%s)", type(exc).__name__)
            return False

    async def request(self, fields: Mapping[str, str]) -> str:
        """Ask the owner; return ``granted``, ``refused`` or ``expired``."""
        nonce = secrets.token_urlsafe(16)
        detail = {"nonce": nonce}
        if self._bot is None or self._owner is None:
            await self._record("approval_refused", {**detail, "reason": "telegram unavailable"})
            return REFUSED
        try:
            text = build_text(fields)
        except ValueError as exc:
            await self._record("approval_refused", {**detail, "reason": str(exc)})
            return REFUSED
        detail["fields"] = dict(fields)
        if not await self._record("approval_requested", detail):
            return REFUSED  # no trail, no request: nothing was sent
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from genesis.channels.telegram.transport.send import safe_send_message

        pending = _Pending(asyncio.get_running_loop().create_future(), detail=detail)
        # Registered before the send: a tap that races the send's response
        # finds the nonce and refuses it rather than being answered "unknown"
        # while the request stays open.
        self._pending[nonce] = pending
        timer = None
        try:
            try:
                msg = await safe_send_message(
                    self._bot,
                    self._owner,
                    text,
                    parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Approve", callback_data=CALLBACK_PREFIX + nonce)]]
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - a failed send is a refusal
                msg, why = None, f"send failed ({type(exc).__name__})"
            else:
                why = "send failed"
            if pending.future.done():  # a tap raced the send; handle_tap recorded it
                return pending.future.result()
            if msg is None or getattr(msg, "text", None) is None:
                self._pending.pop(nonce, None)
                await self._record("approval_refused", {**detail, "reason": why})
                return REFUSED
            pending.chat_id, pending.message_id, pending.text = (
                msg.chat.id,
                msg.message_id,
                msg.text,
            )
            timer = asyncio.get_running_loop().call_later(self._timeout_s, self._expire, nonce)
            outcome = await pending.future
            if outcome == EXPIRED:
                await self._record("approval_expired", detail)
            return outcome
        finally:
            if timer is not None:
                timer.cancel()
            self._pending.pop(nonce, None)

    def _expire(self, nonce: str) -> None:
        pending = self._pending.pop(nonce, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(EXPIRED)

    def _mismatch(self, pending: _Pending, user_id: int | None, message) -> str | None:
        """Why this tap must not grant, or None."""
        if self._owner is None or user_id != self._owner:
            return "not the owner"
        if pending.message_id is None:
            return "tapped before the send returned"
        # InaccessibleMessage (date 0) carries no text or edit state to check.
        if message is None or getattr(message, "is_accessible", False) is not True:
            return "message inaccessible"
        if message.chat.id != pending.chat_id:
            return "other chat"
        if message.message_id != pending.message_id:
            return "other message"
        if message.edit_date is not None:
            return "message edited"
        if message.text != pending.text:
            return "text changed"
        return None

    async def handle_tap(self, nonce: str, user_id: int | None, message) -> bool:
        """Settle one tap; True only for a grant. Any tap consumes the nonce."""
        pending = self._pending.pop(nonce, None)  # before any await: one tap only
        if pending is None:
            return False
        reason = self._mismatch(pending, user_id, message)
        if reason is None and pending.future.done():  # the requester stopped waiting
            reason = "the request is no longer waiting"
        if reason is None and not await self._record("approval_granted", pending.detail):
            reason = "the audit write failed"
        if reason is not None:
            await self._record(
                "approval_refused", {**pending.detail, "reason": reason, "tapper": user_id}
            )
        if pending.future.done():  # cancelled during the audit write: nothing to grant
            return False
        pending.future.set_result(GRANTED if reason is None else REFUSED)
        return reason is None
