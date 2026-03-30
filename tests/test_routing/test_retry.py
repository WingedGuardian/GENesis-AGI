"""Tests for retry logic."""

from __future__ import annotations

from genesis.routing.retry import classify_error, compute_delay
from genesis.routing.types import ErrorCategory, RetryPolicy

# --- classify_error ---

def test_permanent_codes():
    for code in (401, 404):
        assert classify_error(code, "") == ErrorCategory.PERMANENT


def test_quota_codes():
    # 402 is always quota; 403 is quota when message contains quota keywords
    assert classify_error(402, "") == ErrorCategory.QUOTA_EXHAUSTED
    assert classify_error(403, "Key limit exceeded") == ErrorCategory.QUOTA_EXHAUSTED
    assert classify_error(403, "quota exhausted") == ErrorCategory.QUOTA_EXHAUSTED
    # 403 without quota keywords is permanent
    assert classify_error(403, "") == ErrorCategory.PERMANENT
    assert classify_error(403, "access denied") == ErrorCategory.PERMANENT


def test_transient_codes():
    for code in (429, 500, 502, 503, 504):
        assert classify_error(code, "") == ErrorCategory.TRANSIENT


def test_timeout_message():
    assert classify_error(None, "request timeout") == ErrorCategory.TRANSIENT


def test_connection_message():
    assert classify_error(None, "Connection refused") == ErrorCategory.TRANSIENT


def test_malformed_message():
    assert classify_error(None, "malformed response") == ErrorCategory.DEGRADED


def test_partial_message():
    assert classify_error(None, "partial output") == ErrorCategory.DEGRADED


def test_truncated_message():
    assert classify_error(None, "truncated JSON") == ErrorCategory.DEGRADED


def test_unknown_defaults_transient():
    assert classify_error(None, "something weird") == ErrorCategory.TRANSIENT


# --- compute_delay ---

def test_exponential_growth():
    policy = RetryPolicy(base_delay_ms=100, max_delay_ms=100000, jitter_pct=0.0)
    d0 = compute_delay(policy, 0)
    d1 = compute_delay(policy, 1)
    d2 = compute_delay(policy, 2)
    assert d0 < d1 < d2
    # With multiplier=2, no jitter: 100ms, 200ms, 400ms
    assert abs(d0 - 0.1) < 1e-9
    assert abs(d1 - 0.2) < 1e-9
    assert abs(d2 - 0.4) < 1e-9


def test_cap_at_max():
    policy = RetryPolicy(base_delay_ms=1000, max_delay_ms=2000, jitter_pct=0.0)
    d = compute_delay(policy, 10)
    assert abs(d - 2.0) < 1e-9


def test_jitter_within_bounds():
    policy = RetryPolicy(base_delay_ms=1000, max_delay_ms=100000, jitter_pct=0.25)
    for attempt in range(5):
        for _ in range(50):
            d = compute_delay(policy, attempt)
            raw = policy.base_delay_ms * (policy.backoff_multiplier**attempt)
            capped = min(raw, policy.max_delay_ms)
            lo = capped * (1 - policy.jitter_pct) / 1000.0
            hi = capped * (1 + policy.jitter_pct) / 1000.0
            assert lo - 1e-9 <= d <= hi + 1e-9


def test_never_negative():
    policy = RetryPolicy(base_delay_ms=1, max_delay_ms=1, jitter_pct=0.5)
    for _ in range(100):
        assert compute_delay(policy, 0) >= 0.0
