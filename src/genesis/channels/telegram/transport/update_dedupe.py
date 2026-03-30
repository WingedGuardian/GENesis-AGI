"""In-memory deduplication cache for Telegram updates.

Mirrors openclaw-python update_dedupe.py (MIT license):
- TTL: 5 minutes
- Max entries: 2000 (LRU eviction when full)
- Keys: ``update:{update_id}``, ``msg:{chat_id}:{msg_id}``, ``cb:{callback_query_id}``
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

_TTL_SECONDS = 5 * 60  # 5 minutes
_MAX_ENTRIES = 2000


class TelegramUpdateDedupe:
    """Thread-safe (asyncio-safe) LRU+TTL deduplication cache."""

    def __init__(self, ttl: float = _TTL_SECONDS, max_entries: int = _MAX_ENTRIES) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._cache: OrderedDict[str, float] = OrderedDict()

    def is_duplicate(self, key: str) -> bool:
        """Return True if the key was seen recently (within TTL)."""
        self._evict_expired()
        if key in self._cache:
            self._cache.move_to_end(key)
            return True
        return False

    def mark_seen(self, key: str) -> None:
        """Record that key was processed."""
        self._evict_expired()
        if key in self._cache:
            self._cache.move_to_end(key)
            self._cache[key] = time.monotonic()
            return
        if len(self._cache) >= self._max:
            self._cache.popitem(last=False)
        self._cache[key] = time.monotonic()

    def should_skip(self, key: str) -> bool:
        """Idempotent: return True if duplicate, else mark and return False."""
        if self.is_duplicate(key):
            return True
        self.mark_seen(key)
        return False

    def _evict_expired(self) -> None:
        now = time.monotonic()
        cutoff = now - self._ttl
        while self._cache:
            oldest_key, inserted_at = next(iter(self._cache.items()))
            if inserted_at < cutoff:
                self._cache.popitem(last=False)
            else:
                break


def update_key(update_id: int) -> str:
    return f"update:{update_id}"


def message_key(chat_id: int | str, message_id: int | str) -> str:
    return f"msg:{chat_id}:{message_id}"


def callback_key(callback_query_id: str) -> str:
    return f"cb:{callback_query_id}"
