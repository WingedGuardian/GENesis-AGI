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
through the vault service's own connection (``attach_audit``). Rows are
value-light: the shown fields only as lengths, plus the caller's plain
``audit_detail``. Nothing with 12 or more digits in a run is sent or recorded
(``holds_long_digit_run``). It fails
closed: with no audit, or no bot attached (Telegram off or failed), every
request is refused, and a grant whose audit row cannot be written is refused.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
# The card-number guard for everything this module sends or records. A value is
# refused when it holds 12 or more digits (any Unicode digit, ``str.isdigit``,
# so fullwidth and other scripts count) with nothing between them but
# characters that are neither letters nor digits: any space, dash, dot or other
# separator keeps the run going, a letter ends it. Deliberately stricter than
# ``security.card_shapes`` (no brand, length or Luhn test, any separator), so a
# 12+ digit order number, phone number or timestamp in a field is refused too.
# That fails safe: the caller shows less, never a card number.
_DIGIT_RUN_LIMIT = 12
_NONCE_SHAPE = re.compile(r"[A-Za-z0-9_-]{1,64}")

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


def holds_long_digit_run(text: str) -> bool:
    """True if ``text`` has ``_DIGIT_RUN_LIMIT`` digits joined only by non-alphanumerics."""
    run = 0
    for ch in text:
        if ch.isdigit():
            run += 1
            if run >= _DIGIT_RUN_LIMIT:
                return True
        elif ch.isalnum():
            run = 0
    return False


def _safe_str(value: str) -> str:
    # One line per value: a newline or other control character could draw a
    # line that reads as part of this message's own text.
    if not value.isprintable():
        raise ValueError("a value holds a control character")
    if holds_long_digit_run(value):
        raise ValueError("a value holds 12 or more digits")
    return value


def build_text(fields: Mapping[str, str]) -> str:
    """The approval message. Raises ValueError on a field it will not show."""
    if not fields:
        raise ValueError("no fields")
    lines = ["Payment approval"]
    for key, value in fields.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or not value:
            raise ValueError("fields must be non-empty strings")
        lines.append(_safe_str(f"{key}: {value}"))
    lines.append("Tap Approve to release the card for this payment, once.")
    text = "\n".join(lines)
    if _utf16_len(text) > _TG_MAX_UTF16:
        raise ValueError("the message is over Telegram's 4096-character limit")
    return text


def _audit_detail(detail: Mapping | None) -> dict:
    """The caller's value-light audit facts: str, int, bool or None, one level of dict.

    For ``card_ref``, host, amount, the label or last four digits already shown,
    and the requesting process. Free text (a page title) does not belong here;
    the shown fields are recorded only as lengths.
    """
    def clean(value, depth=0):
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, str):
            return _safe_str(value)
        if isinstance(value, Mapping) and depth == 0:
            return {_safe_str(str(k)): clean(v, 1) for k, v in value.items()}
        raise ValueError("an audit value is not a plain value")

    return {_safe_str(str(k)): clean(v) for k, v in (detail or {}).items()}


class PaymentApprovalRegistry:
    """Pending card-release approvals, in memory, matched to the message sent."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._bot = None
        self._owner: int | None = None
        self._audit: Audit | None = None
        self._stopping: Callable[[], bool] = lambda: False
        self._pending: dict[str, _Pending] = {}

    def attach_audit(self, audit: Audit) -> None:
        self._audit = audit

    def attach_bot(self, bot, owner_id: int, *, stopping: Callable[[], bool] | None = None) -> None:
        """``stopping`` is the adapter's shutdown flag, so the send heals a closed client."""
        self._bot, self._owner = bot, owner_id
        self._stopping = stopping or (lambda: False)

    async def _record(self, event: str, detail: dict) -> bool:
        if self._audit is None:
            return False
        try:
            return bool(await self._audit(event, detail))
        except Exception as exc:  # noqa: BLE001 - a lost row is a refusal, never a crash
            logger.error("payment approval audit failed (%s)", type(exc).__name__)
            return False

    async def request(self, fields: Mapping[str, str], *, audit_detail: Mapping | None = None) -> str:
        """Ask the owner; return ``granted``, ``refused`` or ``expired``.

        ``fields`` are shown in the message; ``audit_detail`` is recorded (see
        ``_audit_detail``). Both pass the digit-run guard or the request is refused.
        """
        nonce = secrets.token_urlsafe(16)
        detail: dict = {"nonce": nonce}
        if self._bot is None or self._owner is None:
            await self._record("approval_refused", {**detail, "reason": "telegram unavailable"})
            return REFUSED
        try:
            text = build_text(fields)
            detail.update(_audit_detail(audit_detail))
        except ValueError as exc:  # the messages are this module's own fixed texts
            await self._record("approval_refused", {**detail, "reason": str(exc)})
            return REFUSED
        detail["nonce"] = nonce
        detail["field_lengths"] = {k: len(v) for k, v in fields.items()}
        if not await self._record("approval_requested", detail):
            return REFUSED  # no trail, no request: nothing was sent
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from genesis.channels.telegram.transport.send import (
            safe_send_message,
            send_with_client_heal,
        )

        pending = _Pending(asyncio.get_running_loop().create_future(), detail=detail)
        # Registered before the send: a tap that races the send's response
        # finds the nonce and refuses it rather than being answered "unknown"
        # while the request stays open.
        self._pending[nonce] = pending
        try:
            try:
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Approve", callback_data=CALLBACK_PREFIX + nonce)]]
                )
                # The adapter's own path: rebuilds a shutdown-closed client once.
                msg = await send_with_client_heal(
                    self._bot,
                    lambda: safe_send_message(
                        self._bot, self._owner, text, parse_mode=None, reply_markup=markup
                    ),
                    stopping=self._stopping,
                )
            except Exception as exc:  # noqa: BLE001 - a failed send is a refusal
                msg, why = None, f"send failed ({type(exc).__name__})"
            else:
                why = "send failed"
            if pending.future.done():  # a tap raced the send; handle_tap recorded it
                return pending.future.result()
            if msg is None or getattr(msg, "text", None) is None:
                self._pending.pop(nonce, None)
                pending.future.set_result(REFUSED)
                await self._record("approval_refused", {**detail, "reason": why})
                return REFUSED
            pending.chat_id, pending.message_id, pending.text = (
                msg.chat.id,
                msg.message_id,
                msg.text,
            )
            # The deadline belongs to this wait, not to the dict: a tap that
            # popped the nonce and then died cannot leave it waiting forever.
            try:
                return await asyncio.wait_for(asyncio.shield(pending.future), self._timeout_s)
            except TimeoutError:
                if pending.future.done():  # a tap settled it as the deadline fired
                    return pending.future.result()
                pending.future.set_result(EXPIRED)  # a tap still writing its grant row loses
                await self._record("approval_expired", detail)
                return EXPIRED
        finally:
            self._pending.pop(nonce, None)
            if not pending.future.done():  # the requester was cancelled: no grant can land
                pending.future.cancel()
                await self._record("approval_refused", {**detail, "reason": "requester cancelled"})

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
        if pending is None:  # never sent, already settled, or from before a restart
            await self._record(
                "approval_refused",
                {
                    "nonce": nonce if _NONCE_SHAPE.fullmatch(nonce) else None,
                    "reason": "unknown nonce",
                    "tapper": user_id,
                },
            )
            return False
        try:
            reason = self._mismatch(pending, user_id, message)
            if reason is None and pending.future.done():  # the requester stopped waiting
                reason = "the request is no longer waiting"
            if reason is None:
                if not await self._record("approval_granted", pending.detail):
                    reason = "the audit write failed"
                elif pending.future.done():  # expired or gone while the row was written
                    reason = "requester gone after grant row"
            if reason is None:
                pending.future.set_result(GRANTED)
                return True
            await self._record(
                "approval_refused", {**pending.detail, "reason": reason, "tapper": user_id}
            )
            return False
        finally:
            # A cancelled or failed tap still settles the request it consumed.
            if not pending.future.done():
                pending.future.set_result(REFUSED)


async def start_payment_services(runtime):
    """For ``serve()``: the registry, then the vault service whose connection audits it."""
    from genesis.payments.vault_service import start_vault_service

    registry = PaymentApprovalRegistry()
    return registry, await start_vault_service(runtime, registry)


def attach_telegram(registry: PaymentApprovalRegistry | None, adapter, config: dict) -> None:
    """After Telegram starts: attach its bot, with the owner = first allowlist id as written."""
    owner = (config.get("allowed_users_ordered") or (None,))[0]
    if registry is not None and owner is not None and getattr(adapter, "_app", None) is not None:
        registry.attach_bot(adapter._app.bot, owner, stopping=lambda: adapter._stopping)
