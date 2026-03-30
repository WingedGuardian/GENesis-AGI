"""Typing indicator circuit breaker for Telegram.

Prevents infinite loops when sendChatAction returns 401 or other persistent
errors. After N consecutive failures, suspends typing for exponential backoff.

Reference: OpenClaw TS sendchataction-401-backoff.ts
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_FAILURES = 10
_MIN_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 300.0  # 5 minutes


class TypingCircuitBreaker:
    """Circuit breaker for send_chat_action calls."""

    def __init__(
        self,
        max_failures: int = _MAX_CONSECUTIVE_FAILURES,
        min_backoff_s: float = _MIN_BACKOFF_S,
        max_backoff_s: float = _MAX_BACKOFF_S,
    ) -> None:
        self._max_failures = max_failures
        self._min_backoff = min_backoff_s
        self._max_backoff = max_backoff_s
        # Per-chat state: chat_id → (consecutive_failures, suspended_until)
        self._state: dict[int | str, tuple[int, float]] = {}

    def should_send(self, chat_id: int | str) -> bool:
        """Check if typing indicator should be sent for this chat."""
        failures, suspended_until = self._state.get(chat_id, (0, 0.0))
        return not (failures >= self._max_failures and time.monotonic() < suspended_until)

    def record_success(self, chat_id: int | str) -> None:
        """Record a successful send_chat_action."""
        if chat_id in self._state:
            del self._state[chat_id]

    def record_failure(self, chat_id: int | str) -> None:
        """Record a failed send_chat_action."""
        failures, _ = self._state.get(chat_id, (0, 0.0))
        failures += 1
        if failures >= self._max_failures:
            backoff = min(
                self._min_backoff * (2 ** (failures - self._max_failures)),
                self._max_backoff,
            )
            suspended_until = time.monotonic() + backoff
            logger.warning(
                "Typing circuit breaker OPEN for chat %s: %d consecutive failures, "
                "suspended for %.1fs",
                chat_id, failures, backoff,
            )
        else:
            suspended_until = 0.0
        self._state[chat_id] = (failures, suspended_until)

    def reset(self, chat_id: int | str) -> None:
        """Manually reset state for a chat."""
        self._state.pop(chat_id, None)
