#!/usr/bin/env python3
"""Shared monotonic aggregate-deadline arithmetic for review gates."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


class DeadlineExpired(RuntimeError):
    """Raised when an aggregate deadline expires before or during a probe."""


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

    def capped_after(self, seconds: float) -> Deadline:
        """Return a child deadline no later than this one or ``seconds`` away."""
        child = self.monotonic() + seconds
        expires_at = child if self.expires_at is None else min(self.expires_at, child)
        return Deadline(expires_at, self.monotonic)

    def timeout(self, cap: float) -> float:
        remaining = self.remaining()
        if remaining is None:
            return cap
        if remaining <= 0:
            raise DeadlineExpired("aggregate review-gate deadline expired")
        return min(cap, remaining)


def bounded_timeout(
    deadline: float | None,
    cap: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    """Compatibility helper for callers that carry a raw absolute deadline."""
    return Deadline(deadline, monotonic).timeout(cap)


def propagate_deadline_timeout(deadline: object | None, error: BaseException) -> None:
    """Turn an in-flight timeout into aggregate exhaustion for strict callers.

    Helpers retain their legacy fallback when no aggregate deadline was supplied.
    With a deadline, a timeout is part of that deadline contract and must not be
    converted into permissive ``unknown`` evidence.
    """
    if deadline is not None:
        raise DeadlineExpired("aggregate review-gate deadline expired during subprocess") from error
