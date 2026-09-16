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


class CCStreamTruncatedError(CCProcessError):
    """A stream line exceeded the reader limit and took the RESULT with it.

    Separate from a bare ``CCProcessError`` because the RECOVERY differs, not
    because the message does. This is an output-SIZE failure on a session that
    is otherwise healthy: the CLI ran, tools executed, an answer was produced,
    and only the transport lost it. Re-running the prompt from scratch would
    therefore re-execute every tool call the first attempt already made — an
    MCP write, an outreach send — with nothing downstream to dedupe them.

    So conversation.py must re-raise this instead of routing it into
    stale-resume recovery, which exists for a session that no longer resolves.
    Subclasses ``CCProcessError`` so existing handlers keep catching it; the
    distinction only has to be visible to the one place that retries.
    """


class CCParsingError(CCError):
    """Failed to parse CC output as structured JSON."""


class CCSessionError(CCError):
    """Session resume failed (expired/invalid)."""


class CCMCPError(CCError):
    """MCP server error during CC session."""

    def __init__(self, message: str, *, server_name: str | None = None):
        super().__init__(message)
        self.server_name = server_name


class CCNetworkOfflineError(CCError):
    """The network is hard-OFFLINE and the CC endpoint needs the internet.

    Raised by the CCInvoker's pre-spawn network preflight (PR-3 outage
    resilience) when the connectivity sentinel reports a fresh OFFLINE state,
    the parking lever is ``live``, and the target endpoint classifies as WAN
    (Anthropic's API, or any non-LAN provider). It fires *before* the subprocess
    is spawned, so a dead-network dispatch fails in well under a second instead
    of hanging up to ``CCInvocation.timeout_s`` (7200s default) — the 45-55min
    hangs observed in the 2026-07-28 outage.

    A ``CCError`` subclass so every existing terminal handler already catches it;
    it is added to conversation.py's fast-re-raise tuples so a resume turn does
    NOT mis-treat it as a stale-resume failure (which would needlessly fail a
    live CC session).
    """


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
