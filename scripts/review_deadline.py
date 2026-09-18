#!/usr/bin/env python3
"""Shared monotonic aggregate-deadline arithmetic for review gates."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Deadline:
    """An optional absolute deadline measured by one monotonic clock."""

    expires_at: float | None
    monotonic: Callable[[], float] = time.monotonic

    @classmethod
    def after(
        cls,
        seconds: float | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Deadline:
        return cls(None if seconds is None else monotonic() + seconds, monotonic)

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return self.expires_at - self.monotonic()

    def exhausted(self, *, minimum_useful: float = 0.0) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining < minimum_useful

    def timeout(self, cap: float, *, floor: float = 0.001) -> float:
        remaining = self.remaining()
        if remaining is None:
            return cap
        return max(floor, min(cap, remaining))


def bounded_timeout(
    deadline: float | None,
    cap: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    floor: float = 0.001,
) -> float:
    """Compatibility helper for callers that carry a raw absolute deadline."""
    return Deadline(deadline, monotonic).timeout(cap, floor=floor)
