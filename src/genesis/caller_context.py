"""Who is calling: the single accessor for every caller-identity read.

Stdlib only, importable without the Genesis runtime.

Caller identity today is process environment. ``CCInvoker._build_env`` stamps
it on every Claude Code child, and stdio MCP servers inherit it:

- ``GENESIS_CC_SESSION=1``: a Genesis-dispatched session (set on every child,
  channel conversations included).
- ``GENESIS_SESSION_SUPERVISED=1``: an owner-attended conversation.
- ``GENESIS_SESSION_ID``: the Genesis ``cc_sessions.id`` row.
- ``GENESIS_SESSION_ORIGIN``: the dispatching session's origin class.
- ``CLAUDE_CODE_SESSION_ID``: the Claude Code transcript id (the charter key).
- ``CLAUDE_SESSION_ID``: a legacy name current Claude Code no longer sets; only
  the trash tombstone still reads it.

Code must not read those variables itself. It calls :func:`caller_context`,
and a test fails on any direct read under ``src/`` outside this module. The
reason is a shared MCP process: one process serves many callers, so its own
environment says nothing about the caller of a given request, and a reader that
still consulted it would see "no dispatch marker" and pass human-only gates for
everyone. Routing every read through one function gives a request-scoped value
a single place to take precedence.

Contract for the shared-service work that follows (not implemented here):

- A request-scoped context is set with :func:`request_caller`, per request, and
  is reset in ``finally``, so cancellation resets it too.
- The environment fallback below is for in-process and stdio servers only. A
  shared service selects that by its server MODE, never by a credential being
  absent, and rejects a request whose credential is missing or invalid rather
  than letting it fall through to the environment.
- The MODE check belongs INSIDE :func:`caller_context`'s fallback branch, not
  only in request middleware. A context variable does not follow work handed
  to ``threading.Thread`` or ``loop.run_in_executor`` (``asyncio.to_thread``
  does copy it), so such work reaches the fallback. In a shared service that
  fallback must fail closed, never read the shared process's environment. A
  subprocess a tool spawns inherits the service's environment, not the
  caller's, and needs the caller passed to it explicitly.
- An external client's profile is carried in :attr:`CallerContext.profile`, so
  profile and identity are one value and one context variable.
- Two session-id sources exist: ``observability.session_context`` (the
  in-process id genesis-server sets for work it runs) and
  :attr:`CallerContext.session_id`. Readers that consult both put the
  in-process one first today; a shared service decides that order on purpose.

In stdio and in-process mode, :func:`caller_context` returns exactly what the
environment held at the moment of the call. It is read on every call and never
cached, because the same tool functions also run in-process in genesis-server.
Values are kept raw (an empty string stays an empty string) so each reader
keeps its own normalisation unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

CC_SESSION_ENV = "GENESIS_CC_SESSION"
SUPERVISED_ENV = "GENESIS_SESSION_SUPERVISED"
SESSION_ID_ENV = "GENESIS_SESSION_ID"
SESSION_ORIGIN_ENV = "GENESIS_SESSION_ORIGIN"
TRANSCRIPT_ID_ENV = "CLAUDE_CODE_SESSION_ID"
LEGACY_SESSION_ID_ENV = "CLAUDE_SESSION_ID"

IDENTITY_ENV_VARS = frozenset(
    {
        CC_SESSION_ENV,
        SUPERVISED_ENV,
        SESSION_ID_ENV,
        SESSION_ORIGIN_ENV,
        TRANSCRIPT_ID_ENV,
        LEGACY_SESSION_ID_ENV,
    }
)


@dataclass(frozen=True, slots=True)
class CallerContext:
    """The identity of the caller of the current request or process.

    ``dispatched`` and ``supervised`` are the exact-``"1"`` tests the readers
    have always applied. The id and origin fields are raw: ``None`` when unset,
    otherwise the value as found, empty or padded.

    The defaults are the LEAST privileged caller (dispatched, unsupervised), so
    a context built with a field left out fails closed at the human-only gates.
    ``dispatched=False`` asserts a foreground human caller; a shared service
    sets it only for a credential that stands for one. The environment
    fallback sets every field explicitly, so these defaults never reach it.
    """

    dispatched: bool = True
    supervised: bool = False
    session_id: str | None = None
    origin: str | None = None
    transcript_id: str | None = None
    legacy_session_id: str | None = None
    profile: str | None = None

    @property
    def unsupervised_dispatched(self) -> bool:
        """A dispatched session with no owner attending: the human-gate test."""
        return self.dispatched and not self.supervised


_REQUEST_CALLER: ContextVar[CallerContext | None] = ContextVar(
    "genesis_request_caller", default=None
)


def caller_context_from_env() -> CallerContext:
    """The caller as this process's environment describes it, read now."""
    env = os.environ
    return CallerContext(
        dispatched=env.get(CC_SESSION_ENV) == "1",
        supervised=env.get(SUPERVISED_ENV) == "1",
        session_id=env.get(SESSION_ID_ENV),
        origin=env.get(SESSION_ORIGIN_ENV),
        transcript_id=env.get(TRANSCRIPT_ID_ENV),
        legacy_session_id=env.get(LEGACY_SESSION_ID_ENV),
    )


def caller_context() -> CallerContext:
    """The current caller: the request-scoped value if one is set, else the environment."""
    request = _REQUEST_CALLER.get()
    if request is not None:
        return request
    return caller_context_from_env()


@contextmanager
def request_caller(context: CallerContext) -> Iterator[CallerContext]:
    """Make *context* the caller for the enclosed block, and restore the previous one on any exit."""
    token = _REQUEST_CALLER.set(context)
    try:
        yield context
    finally:
        _REQUEST_CALLER.reset(token)
