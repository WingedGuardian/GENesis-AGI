"""ReplyWaiter — send-and-wait infrastructure for bidirectional outreach.

Maintains a registry of asyncio.Future objects keyed by delivery_id.
When outreach sends a message and wants to wait for a user reply, it
registers a waiter. When the Telegram handler detects a quote-reply
to that message, it resolves the waiter with the reply text.

Waiters may carry a *thread context* ("chat_id:thread_id" of the delivered
message). That context is what makes standalone-text resolution safe:
``resolve_scoped_pending`` only resolves a waiter when the user's message
arrived in the SAME chat+topic the prompt was delivered to — the
cross-chat conflation that got the old ``resolve_any_pending`` disabled
(a DM once resolved an alert-topic approval) is structurally impossible.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Waiting on a HUMAN, not a machine: 300s was the demonstrated failure mode
# (buttons tapped minutes later hit a vanished waiter — the "button TTL
# stall"). Project timeout policy floor is 2 hours; a pending waiter holds
# one future in a dict, nothing else.
DEFAULT_REPLY_TIMEOUT_S = 7200.0


class ReplyWaiter:
    """Registry for pending outreach reply futures."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[str]] = {}
        # delivery_id -> "chat_id:thread_id" of the delivered prompt.
        self._contexts: dict[str, str] = {}

    def register(self, delivery_id: str) -> asyncio.Future[str]:
        """Register a waiter for a delivery_id. Returns the Future."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._waiters[delivery_id] = future
        logger.info("Registered reply waiter for delivery %s", delivery_id)
        return future

    def set_context(self, delivery_id: str, thread_key: str) -> bool:
        """Attach the delivered message's "chat_id:thread_id" to a waiter.

        Called by the pipeline after delivery, when the destination is
        known. Waiters without a context are never eligible for
        standalone-text resolution (quote-reply and buttons still work).

        Returns False when *delivery_id* has no registered waiter — the
        context is DROPPED in that case, so callers must register first
        (the silent drop is exactly the bug that left send-and-wait
        waiters unresolvable by standalone replies).
        """
        if delivery_id in self._waiters:
            self._contexts[delivery_id] = thread_key
            return True
        return False

    def resolve(self, delivery_id: str, reply_text: str) -> bool:
        """Resolve a waiter with reply text. Returns True if waiter existed."""
        future = self._waiters.pop(delivery_id, None)
        self._contexts.pop(delivery_id, None)
        if future is None or future.done():
            return False
        future.set_result(reply_text)
        logger.info("Resolved reply waiter for delivery %s", delivery_id)
        return True

    def add_alias(self, alias_id: str, canonical_id: str) -> None:
        """Map *alias_id* to the same future as *canonical_id*.

        Used so that both a pre-generated UUID (in button callback_data) and
        the Telegram message_id (for quote-reply fallback) resolve the same
        waiter. The alias inherits the canonical context, if set.
        """
        future = self._waiters.get(canonical_id)
        if future is not None:
            self._waiters[alias_id] = future
            context = self._contexts.get(canonical_id)
            if context is not None:
                self._contexts[alias_id] = context

    def remove(self, *keys: str) -> None:
        """Remove one or more keys from the registry without resolving."""
        for key in keys:
            self._waiters.pop(key, None)
            self._contexts.pop(key, None)

    def cancel(self, delivery_id: str) -> None:
        """Cancel a pending waiter."""
        future = self._waiters.pop(delivery_id, None)
        self._contexts.pop(delivery_id, None)
        if future and not future.done():
            future.cancel()

    def resolve_scoped_pending(self, reply_text: str, *, thread_key: str) -> list[str]:
        """Resolve a pending waiter with a standalone (non-quote-reply)
        message — but ONLY within the message's own chat+topic.

        Eligible waiters are those whose recorded context equals
        *thread_key*. Resolves only when exactly ONE distinct pending
        future is eligible; zero or several (ambiguous) resolve nothing.
        Contextless waiters are never eligible — guessing the destination
        is exactly the bug that got the unscoped version disabled.

        Returns the resolved waiter's registry keys (canonical + aliases —
        e.g. a pre-generated UUID and the Telegram message_id) so the
        caller can correlate the reply back to the delivered outreach
        record; empty list when nothing was resolved. Any single key may
        be an alias, so callers should try each against the store.
        """
        eligible: dict[asyncio.Future[str], str] = {}
        for did, future in self._waiters.items():
            if future.done():
                continue
            if self._contexts.get(did) != thread_key:
                continue
            eligible.setdefault(future, did)  # aliases share one future
        if len(eligible) != 1:
            return []
        future, delivery_id = next(iter(eligible.items()))
        keys = self._pop_future(future)
        future.set_result(reply_text)
        logger.info(
            "Resolved pending reply waiter %s via standalone message in %s",
            delivery_id, thread_key,
        )
        return keys

    def _pop_future(self, future: asyncio.Future[str]) -> list[str]:
        """Drop every key (canonical + aliases) mapped to *future*.

        Returns the dropped keys so callers can correlate the resolved
        waiter back to a delivered message (registry keys are the only
        link — they live in memory only).
        """
        keys = [k for k, f in self._waiters.items() if f is future]
        for key in keys:
            self._waiters.pop(key, None)
            self._contexts.pop(key, None)
        return keys

    def resolve_any_pending(self, reply_text: str) -> bool:
        """UNSCOPED single-pending resolution — kept for API compatibility
        only; it conflates messages across chats/topics (a DM once resolved
        an alert-topic approval). Prefer ``resolve_scoped_pending``.
        """
        pending = [
            (did, f) for did, f in self._waiters.items() if not f.done()
        ]
        if len(pending) != 1:
            return False  # Ambiguous or no waiters — don't resolve
        delivery_id, future = pending[0]
        self._pop_future(future)
        future.set_result(reply_text)
        logger.info(
            "Resolved pending reply waiter %s via standalone message (no quote-reply)",
            delivery_id,
        )
        return True

    @property
    def pending_count(self) -> int:
        """Number of unresolved waiters.

        Note: when aliases exist, this may overcount (same future counted
        twice); informational only.
        """
        return sum(1 for f in self._waiters.values() if not f.done())

    async def wait_for_reply(
        self, delivery_id: str, *, timeout_s: float = DEFAULT_REPLY_TIMEOUT_S,
    ) -> str | None:
        """Wait for a reply to a specific delivery. Returns None on timeout."""
        future = self._waiters.get(delivery_id)
        if future is None:
            future = self.register(delivery_id)
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError:
            self._waiters.pop(delivery_id, None)
            self._contexts.pop(delivery_id, None)
            logger.info("Reply waiter timed out for delivery %s", delivery_id)
            return None
