"""Structured exception hierarchy for CC invocation failures.

Typed exceptions enable per-error handling in conversation.py and
user-facing error templates in channel handlers.
"""

from __future__ import annotations


class CCError(Exception):
    """Base for all CC invocation errors.

    ``oom_suspected`` is the MACHINE-READABLE half of an OOM attribution; the
    human half is the note appended to the message, which no caller should have
    to pattern-match to make a control-flow decision. It exists because an OOM
    death must NOT be handled as a stale resume or as a reason to try the next
    peer: the session is fine, the process was reaped, and re-running the
    identical memory-heavy work re-arms the failure under the same pressure.

    IT LIVES ON THE BASE, not on ``CCProcessError``, and the move is the fix for
    a real hole rather than tidying. A reaped process usually writes nothing, so
    it classified as a generic process error and the flag reached the callers.
    But a process that emitted ANY recognised line before dying — an MCP startup
    error, a session-expiry message, buffered quota text — classifies as
    ``CCMCPError`` / ``CCSessionError`` / ``CCQuotaExhaustedError`` instead, and
    when the flag existed only on ``CCProcessError`` those exits carried NO
    attribution at all. They then took the worst possible branch: the MCP and
    session ones entered stale-resume recovery and re-ran the workload, and the
    quota one could park the turn for a capacity reset that was never the
    problem. The class default is False, so every type answers the question and
    none has to know about OOM.

    A flag rather than a new exception TYPE, deliberately: the retry semantics of
    the underlying failure stay intact everywhere that does not opt in — an OOM
    that also hit a real rate limit is still a rate limit — and only the paths
    that must not re-arm the work key off it.

    See ``genesis.observability.oom`` for how the attribution is derived and for
    the (deliberately narrow) conditions under which it is claimed at all.
    """

    oom_suspected: bool = False


def annotate_oom(err: CCError, note: str) -> CCError:
    """Attach an OOM attribution to an ALREADY-CLASSIFIED error, in place.

    The single place the two halves are applied together, so a call site cannot
    set one and forget the other. Applied AFTER classification rather than
    instead of it: what killed the process and what the process managed to say
    before dying are independent facts, and the old code could only report the
    second.

    ``args[0]`` is replaced rather than a new exception constructed: the typed
    subclasses carry their own keyword state (``server_name``, ``raw_text``,
    ``raw_event``) that a rebuild would have to enumerate and would silently
    drop as new fields are added.
    """
    err.oom_suspected = True
    base = str(err) or "killed with no output"
    err.args = (f"{base} — {note}", *err.args[1:])
    return err


class CCTimeoutError(CCError):
    """CC session timed out."""


class CCProcessError(CCError):
    """CC CLI exited with non-zero status.

    The ``oom_suspected`` keyword is kept here (rather than moved to the base's
    ``__init__``) because the base has none: adding one would force every
    subclass with its own ``__init__`` to forward it.
    """

    def __init__(self, message: str = "", *, oom_suspected: bool = False):
        super().__init__(message)
        self.oom_suspected = oom_suspected


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
