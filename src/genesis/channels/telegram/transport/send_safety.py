"""Pre/post-send error classification for Telegram API calls.

Determines whether a failed send is safe to retry by classifying whether
the error occurred before or after Telegram received the request.

Reference: OpenClaw TS network-errors.ts isSafeToRetrySendError()
Adapted for Python/httpx/PTB exception hierarchy.
"""
from __future__ import annotations

import logging

import httpx
from telegram.error import NetworkError, TimedOut

logger = logging.getLogger(__name__)


def _unwrap(exc: Exception) -> Exception:
    """Unwrap to the innermost cause, checking both explicit and implicit chains.

    ``__cause__`` is set by ``raise X from Y`` (explicit).
    ``__context__`` is set by ``raise X`` inside an ``except`` block (implicit).
    PTB often wraps httpx errors via implicit chaining, so we must check both.
    """
    return exc.__cause__ or exc.__context__ or exc


def is_safe_to_retry_send(exc: Exception, _depth: int = 0) -> bool:
    """True only if error provably occurred before Telegram received the request.

    Safe to retry (pre-connect):
    - httpx.ConnectError: DNS failure, connection refused, unreachable
    - httpx.ConnectTimeout: TCP handshake never completed

    NOT safe to retry (post-connect — message may have been received):
    - httpx.ReadTimeout: request sent, waiting for response timed out
    - httpx.WriteTimeout: partially written to socket
    - TimedOut: PTB wrapper, ambiguous — defaults to unsafe
    - Any other error: assume unsafe
    """
    if _depth > 10:
        return False  # Guard against exception chain cycles

    cause = _unwrap(exc)

    # httpx pre-connect errors — safe to retry
    if isinstance(cause, httpx.ConnectError):
        return True
    if isinstance(cause, httpx.ConnectTimeout):
        return True

    # PTB wraps httpx errors in NetworkError — unwrap and check
    if isinstance(cause, NetworkError):
        inner = _unwrap(cause)
        if inner is not cause:
            return is_safe_to_retry_send(inner, _depth + 1)

    # Recurse through exception chain (both explicit and implicit)
    inner = _unwrap(cause)
    if inner is not cause:
        return is_safe_to_retry_send(inner, _depth + 1)

    return False


def classify_send_error(exc: Exception) -> str:
    """Classify a send error for logging. Returns a short category string."""
    cause = _unwrap(exc)
    if isinstance(cause, httpx.ConnectError):
        return "pre_connect"
    if isinstance(cause, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(cause, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(cause, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(cause, TimedOut):
        return "ptb_timeout"
    if isinstance(cause, NetworkError):
        return "network_error"
    return "unknown"
