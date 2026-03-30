"""Retry logic — error classification and delay computation."""

from __future__ import annotations

import random

from genesis.routing.types import ErrorCategory, RetryPolicy

_TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}
_PERMANENT_CODES = {401, 404}
_QUOTA_CODES = {402}  # 402 Payment Required is always quota
_MAYBE_QUOTA_CODES = {403}  # 403 is quota only if message contains keywords
_QUOTA_KEYWORDS = frozenset({
    "quota", "exceeded", "billing", "limit", "exhausted",
    "usage", "credits", "subscription", "plan",
})


def classify_error(status_code: int | None, error_msg: str) -> ErrorCategory:
    """Classify an error into a category for routing decisions."""
    if status_code is not None:
        if status_code in _QUOTA_CODES:
            return ErrorCategory.QUOTA_EXHAUSTED
        if status_code in _MAYBE_QUOTA_CODES:
            if any(kw in error_msg.lower() for kw in _QUOTA_KEYWORDS):
                return ErrorCategory.QUOTA_EXHAUSTED
            return ErrorCategory.PERMANENT
        if status_code in _PERMANENT_CODES:
            return ErrorCategory.PERMANENT
        if status_code in _TRANSIENT_CODES:
            return ErrorCategory.TRANSIENT

    msg = error_msg.lower()
    if "timeout" in msg or "connection" in msg:
        return ErrorCategory.TRANSIENT
    if "malformed" in msg or "partial" in msg or "truncated" in msg:
        return ErrorCategory.DEGRADED

    return ErrorCategory.TRANSIENT


def compute_delay(policy: RetryPolicy, attempt: int) -> float:
    """Compute retry delay in seconds with exponential backoff and jitter."""
    raw = policy.base_delay_ms * (policy.backoff_multiplier**attempt)
    capped = min(raw, policy.max_delay_ms)
    jitter = capped * policy.jitter_pct
    delay_ms = capped + random.uniform(-jitter, jitter)  # noqa: S311
    return max(0.0, delay_ms / 1000.0)
