"""Structured exception hierarchy for CC invocation failures.

Typed exceptions enable per-error handling in conversation.py and
user-facing error templates in channel handlers.
"""

from __future__ import annotations


class CCError(Exception):
    """Base for all CC invocation errors."""


class CCTimeoutError(CCError):
    """CC session timed out."""


class CCProcessError(CCError):
    """CC CLI exited with non-zero status."""


class CCParsingError(CCError):
    """Failed to parse CC output as structured JSON."""


class CCSessionError(CCError):
    """Session resume failed (expired/invalid)."""


class CCMCPError(CCError):
    """MCP server error during CC session."""

    def __init__(self, message: str, *, server_name: str | None = None):
        super().__init__(message)
        self.server_name = server_name


class _CCLimitError(CCError):
    """Shared base for rate-limit / quota errors — carries the reset signal.

    The CC CLI surfaces a reset hint ("resets Xpm") in the error prose and, on
    the streaming path, a structured ``rate_limit_event`` payload. Both were
    historically discarded at every detection point. Capturing them here lets
    the durability layer (``cc_rate_limit_parks``) schedule a resume off the
    real reset time instead of a blind backoff. Both fields are optional so all
    existing single-arg raises (``CCRateLimitError(text)``) stay valid.
    """

    def __init__(
        self,
        message: str = "",
        *,
        raw_event: dict | None = None,
        raw_text: str | None = None,
    ):
        super().__init__(message)
        self.raw_event = raw_event
        self.raw_text = raw_text


class CCRateLimitError(_CCLimitError):
    """CC hit transient rate limit (recovers in minutes)."""


class CCQuotaExhaustedError(_CCLimitError):
    """CC usage quota exhausted — hard ceiling lasting hours.

    Distinct from CCRateLimitError: quota exhaustion means the Max subscription
    hit its usage limit, not a transient 429. CC will be unavailable for hours.
    """
