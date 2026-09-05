"""Retry logic — error classification and delay computation."""

from __future__ import annotations

import random

from genesis.routing.types import ErrorCategory, RetryPolicy

_TRANSIENT_CODES = {500, 502, 503, 504}
_RATE_LIMITED_CODES = {429}  # provider backpressure — fail fast, do NOT trip the breaker
_BAD_REQUEST_CODES = {400, 422}  # deterministic client errors — no retry, no trip
_TIMEOUT_CODES = {408}  # 408 Request Timeout — litellm.Timeout maps here
_PERMANENT_CODES = {401, 404}
_QUOTA_CODES = {402}  # 402 Payment Required is always quota
_MAYBE_QUOTA_CODES = {403}  # 403 is quota only if message contains keywords
_QUOTA_KEYWORDS = frozenset({
    "quota", "exceeded", "billing", "limit", "exhausted",
    "usage", "credits", "subscription", "plan",
})
# A 403 that means "your tier may not use this model" — checked BEFORE
# _QUOTA_KEYWORDS, because the two vocabularies overlap and quota would
# otherwise win: an entitlement message naturally names the thing you do not
# have ("subscription", "plan", "tier"), and three of those words are quota
# keywords. Order is the whole fix; see ErrorCategory.NOT_ENTITLED for why the
# distinction has to exist at all.
#
# Provenance, because these are matched against live vendor prose. The first
# two are MEASURED verbatim from Mistral (2026-09-05, and the same shape in the
# outage from 2026-08-27); zero false positives over 1,093 real error messages
# in `activity_log` that day, with OpenRouter's genuine "Key limit exceeded"
# quota 403 correctly NOT captured. The remaining four are INFERRED, and are of
# two different kinds rather than one:
#   - negation beside an access noun ("does not have access", "not authorized")
#     — consumption language ("exceeded your usage") cannot reach these.
#   - a vendor status TOKEN ("permission_denied"). Broader than entitlement
#     proper: Google returns it at 403 for project-level states too (API not
#     enabled, suspended consumer). Those want the same fail-fast and tolerate
#     the same long cap, so the over-capture is acceptable — but it is
#     over-capture, not a precise match, and is recorded as such.
# KNOWN GAP, measured not assumed: the OpenAI-family phrasing that carries the
# "access" wording ("The model X does not exist or you do not have access to
# it") arrives as 404, which `litellm_delegate` maps to `status_code=404` and
# `_PERMANENT_CODES` sends straight to PERMANENT — so those two markers are
# UNCONFIRMED at 403 and may never fire here. Widening this branch to 404 on
# inference alone was rejected; add a marker when a real one is measured.
_ENTITLEMENT_MARKERS = (
    "tier_not_allowed",
    "not available in your",
    "does not have access",
    "do not have access",
    "not authorized",
    "permission_denied",
)


def classify_error(status_code: int | None, error_msg: str) -> ErrorCategory:
    """Classify an error into a category for routing decisions."""
    if status_code is not None:
        if status_code in _QUOTA_CODES:
            return ErrorCategory.QUOTA_EXHAUSTED
        if status_code in _MAYBE_QUOTA_CODES:
            msg_lower = error_msg.lower()
            if any(m in msg_lower for m in _ENTITLEMENT_MARKERS):
                return ErrorCategory.NOT_ENTITLED
            if any(kw in msg_lower for kw in _QUOTA_KEYWORDS):
                return ErrorCategory.QUOTA_EXHAUSTED
            return ErrorCategory.PERMANENT
        if status_code in _PERMANENT_CODES:
            return ErrorCategory.PERMANENT
        if status_code in _BAD_REQUEST_CODES:
            return ErrorCategory.BAD_REQUEST
        if status_code in _RATE_LIMITED_CODES:
            return ErrorCategory.RATE_LIMITED
        if status_code in _TIMEOUT_CODES:
            return ErrorCategory.TIMEOUT
        if status_code in _TRANSIENT_CODES:
            return ErrorCategory.TRANSIENT

    msg = error_msg.lower()
    if "timeout" in msg:
        return ErrorCategory.TIMEOUT
    if "connection" in msg:
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
