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


class CCRateLimitError(CCError):
    """CC hit transient rate limit (recovers in minutes)."""


class CCQuotaExhaustedError(CCError):
    """CC usage quota exhausted — hard ceiling lasting hours.

    Distinct from CCRateLimitError: quota exhaustion means the Max subscription
    hit its usage limit, not a transient 429. CC will be unavailable for hours.
    """
