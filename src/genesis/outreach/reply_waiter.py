"""ReplyWaiter — send-and-wait infrastructure for bidirectional outreach.

Maintains a registry of asyncio.Future objects keyed by delivery_id.
When outreach sends a message and wants to wait for a user reply, it
registers a waiter. When the Telegram handler detects a quote-reply
to that message, it resolves the waiter with the reply text.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ReplyWaiter:
    """Registry for pending outreach reply futures."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[str]] = {}

    def register(self, delivery_id: str) -> asyncio.Future[str]:
        """Register a waiter for a delivery_id. Returns the Future."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._waiters[delivery_id] = future
        logger.info("Registered reply waiter for delivery %s", delivery_id)
        return future

    def resolve(self, delivery_id: str, reply_text: str) -> bool:
        """Resolve a waiter with reply text. Returns True if waiter existed."""
        future = self._waiters.pop(delivery_id, None)
        if future is None or future.done():
            return False
        future.set_result(reply_text)
        logger.info("Resolved reply waiter for delivery %s", delivery_id)
        return True

    def add_alias(self, alias_id: str, canonical_id: str) -> None:
        """Map *alias_id* to the same future as *canonical_id*.

        Used so that both a pre-generated UUID (in button callback_data) and
        the Telegram message_id (for quote-reply fallback) resolve the same
        waiter.
        """
        future = self._waiters.get(canonical_id)
        if future is not None:
            self._waiters[alias_id] = future

    def remove(self, *keys: str) -> None:
        """Remove one or more keys from the registry without resolving."""
        for key in keys:
            self._waiters.pop(key, None)

    def cancel(self, delivery_id: str) -> None:
        """Cancel a pending waiter."""
        future = self._waiters.pop(delivery_id, None)
        if future and not future.done():
            future.cancel()

    def resolve_any_pending(self, reply_text: str) -> bool:
        """Resolve the most recent pending waiter with reply text.

        Used when a user sends a standalone message (not a quote-reply)
        and there's exactly one pending waiter — no ambiguity about which
        message they're responding to. Returns True if a waiter was resolved.
        """
        pending = [
            (did, f) for did, f in self._waiters.items() if not f.done()
        ]
        if len(pending) != 1:
            return False  # Ambiguous or no waiters — don't resolve
        delivery_id, future = pending[0]
        self._waiters.pop(delivery_id, None)
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
        twice). This is acceptable since resolve_any_pending is disabled.
        """
        return sum(1 for f in self._waiters.values() if not f.done())

    async def wait_for_reply(
        self, delivery_id: str, *, timeout_s: float = 300.0,
    ) -> str | None:
        """Wait for a reply to a specific delivery. Returns None on timeout."""
        future = self._waiters.get(delivery_id)
        if future is None:
            future = self.register(delivery_id)
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError:
            self._waiters.pop(delivery_id, None)
            logger.info("Reply waiter timed out for delivery %s", delivery_id)
            return None
