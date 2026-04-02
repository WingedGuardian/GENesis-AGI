"""Per-provider rate gate — enforces RPM limits to prevent thundering herd.

When multiple call sites cascade to the same fallback provider simultaneously
(e.g., Mistral rate-limits and everyone hits Groq at once), the rate gate
serializes requests to respect each provider's RPM limit.

Uses asyncio locks per provider — requests queue naturally rather than
all firing at once and tripping circuit breakers.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class ProviderRateGate:
    """Enforces minimum interval between requests to a single provider.

    Based on the provider's RPM limit: interval = 60 / rpm_limit seconds.
    Thread-safe via asyncio.Lock (one request at a time per provider).
    """

    __slots__ = ("_interval", "_last_request", "_lock", "_provider")

    def __init__(self, provider: str, rpm: int) -> None:
        self._provider = provider
        self._interval = 60.0 / rpm
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Wait until it's safe to send a request. Returns wait time in seconds."""
        async with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            return max(wait, 0.0)

    @property
    def interval(self) -> float:
        return self._interval


class RateGateRegistry:
    """Manages rate gates for all providers with RPM limits."""

    def __init__(self) -> None:
        self._gates: dict[str, ProviderRateGate] = {}

    def register(self, provider: str, rpm: int) -> None:
        """Register a rate gate for a provider. Overwrites if already exists."""
        self._gates[provider] = ProviderRateGate(provider, rpm)
        logger.debug(
            "Rate gate registered for %s: %d RPM (%.1fs interval)",
            provider, rpm, 60.0 / rpm,
        )

    async def acquire(self, provider: str) -> float:
        """Acquire rate gate for provider. Returns 0.0 if no gate configured."""
        gate = self._gates.get(provider)
        if gate is None:
            return 0.0
        return await gate.acquire()

    def has_gate(self, provider: str) -> bool:
        return provider in self._gates

    def __len__(self) -> int:
        return len(self._gates)
