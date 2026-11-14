"""Who is calling: the single accessor for every caller-identity read.

Stdlib only, importable without the Genesis runtime.

Caller identity today is process environment. ``CCInvoker._build_env`` stamps
it on every Claude Code child, and stdio MCP servers inherit it:

- ``GENESIS_CC_SESSION=1``: a Genesis-dispatched session (set on every child,
  channel conversations included, and by :func:`dispatched_child_env` on the
  agent CLIs Genesis starts unattended, such as ``codex exec``).
- ``GENESIS_SESSION_SUPERVISED=1``: an owner-attended conversation.
- ``GENESIS_SESSION_ID``: the Genesis ``cc_sessions.id`` row.
- ``GENESIS_SESSION_ORIGIN``: the dispatching session's origin class.
- ``CLAUDE_CODE_SESSION_ID``: the Claude Code transcript id (the charter key).
- ``CLAUDE_SESSION_ID``: a legacy name current Claude Code no longer sets; only
  the trash tombstone still reads it.
- ``GENESIS_CALLER_PROFILE``: the client a stdio server serves, set by that
  client's launcher (``codex`` for owner-mode Codex). Read into
  :attr:`CallerContext.profile`; it labels what the client did and never grants
  anything.
- ``CLAUDECODE``: set by Claude Code in the shells and MCP servers it starts.
- ``CODEX_THREAD_ID``: set by Codex in the shells it runs commands in (not in
  its MCP servers, which get a filtered environment).

The last two feed :func:`client_kind`, a label for records, never a permission.

Code must not read those variables itself. It calls :func:`caller_context`,
and a test fails on any direct read under ``src/`` outside this module. The
reason is a shared MCP process: one process serves many callers, so its own
environment says nothing about the caller of a given request, and a reader that
still consulted it would see "no dispatch marker" and pass human-only gates for
everyone. Routing every read through one function gives a request-scoped value
a single place to take precedence.

Contract for the shared-service work that follows (the service itself is not
implemented here):

- A request-scoped context is set with :func:`request_caller`, per request, and
  is reset in ``finally``, so cancellation resets it too.
- The environment fallback below is for in-process and stdio servers only. A
  shared service selects that by its server MODE, never by a credential being
  absent, and rejects a request whose credential is missing or invalid rather
  than letting it fall through to the environment.
- The MODE check is INSIDE :func:`caller_context`'s fallback branch, not only
  in request middleware. A context variable does not follow work handed to
  ``threading.Thread`` or ``loop.run_in_executor`` (``asyncio.to_thread`` does
  copy it), so such work reaches the fallback. A shared service calls
  :func:`enter_shared_service` after its runtime is initialised and before it
  serves its first request; from then on that fallback fails closed and never
  reads the shared process's environment. A subprocess a tool spawns inherits the service's environment, not the
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

import logging
import os
import sys
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CC_SESSION_ENV = "GENESIS_CC_SESSION"
SUPERVISED_ENV = "GENESIS_SESSION_SUPERVISED"
SESSION_ID_ENV = "GENESIS_SESSION_ID"
SESSION_ORIGIN_ENV = "GENESIS_SESSION_ORIGIN"
TRANSCRIPT_ID_ENV = "CLAUDE_CODE_SESSION_ID"
LEGACY_SESSION_ID_ENV = "CLAUDE_SESSION_ID"
# The client a stdio server is serving, set by that client's launcher (``codex``
# for owner-mode Codex). A record of which client acted, never a permission.
PROFILE_ENV = "GENESIS_CALLER_PROFILE"
CLAUDE_CODE_ENV = "CLAUDECODE"
CODEX_THREAD_ENV = "CODEX_THREAD_ID"

IDENTITY_ENV_VARS = frozenset(
    {
        CC_SESSION_ENV,
        SUPERVISED_ENV,
        SESSION_ID_ENV,
        SESSION_ORIGIN_ENV,
        TRANSCRIPT_ID_ENV,
        LEGACY_SESSION_ID_ENV,
        PROFILE_ENV,
        CLAUDE_CODE_ENV,
        CODEX_THREAD_ENV,
    }
)

# External launchers clear caller identity and the existing dispatch tracing tags.
EXTERNAL_IDENTITY_ENV_VARS = IDENTITY_ENV_VARS | {
    "GENESIS_SLOT", "GENESIS_TRACE_ID", "GENESIS_PARENT_SPAN_ID",
}


@dataclass(frozen=True, slots=True)
class CallerContext:
    """The identity of the caller of the current request or process.

    ``dispatched`` and ``supervised`` are the exact-``"1"`` tests the readers
    have always applied. The id and origin fields are raw: ``None`` when unset,
    otherwise the value as found, empty or padded.

    The defaults are the LEAST privileged caller (dispatched, unsupervised), so
    a context built with a field left out fails closed at the human-only gates.
    ``dispatched=False`` asserts a foreground human caller; a shared service
    sets it only for a credential that stands for one. ``origin`` defaults to
    ``None``, which memory writers read as first-party, so a caller of unknown
    trust must set it (see :data:`_LOST_SCOPE_CALLER`).
    """

    dispatched: bool = True
    supervised: bool = False
    session_id: str | None = None
    origin: str | None = None
    transcript_id: str | None = None
    legacy_session_id: str | None = None
    profile: str | None = None
    claude_code: str | None = None
    codex_thread: str | None = None

    @property
    def unsupervised_dispatched(self) -> bool:
        """A dispatched session with no owner attending: the human-gate test."""
        return self.dispatched and not self.supervised


_REQUEST_CALLER: ContextVar[CallerContext | None] = ContextVar(
    "genesis_request_caller", default=None
)
_shared_service = False
#: Work that lost its request scope. The literal is provenance's
#: ``ORIGIN_EXTERNAL_UNTRUSTED``; provenance imports this module, so a test pins it.
_LOST_SCOPE_CALLER = CallerContext(origin="external_untrusted")
_warned_sites: set[tuple[str, int]] = set()


def enter_shared_service() -> None:
    """Mark this process as a shared service, for good: its environment names no caller.

    Rewrites the identity variables as well, so a child process, which inherits
    the environment and not this flag, reads the lost-scope caller too unless a
    tool passes it the real caller. Bootstrap clears the origin variable, so
    call this after the runtime is initialised. The agent-shell markers go
    too, so a child no longer reads as running inside an agent's shell: a
    command-line tool that refuses there (the browser-stack install) needs the
    caller passed to it like any other identity.
    """
    global _shared_service
    _shared_service = True
    for name in IDENTITY_ENV_VARS:
        os.environ.pop(name, None)
    os.environ[CC_SESSION_ENV] = "1"
    os.environ[SESSION_ORIGIN_ENV] = _LOST_SCOPE_CALLER.origin


def caller_context_from_env() -> CallerContext:
    """Read legacy process identity; shared processes have no environment caller.

    Shared mode returns the lost-scope caller even inside a request. Use
    ``caller_context()`` for the validated request's identity instead.
    """
    if _shared_service:
        return _LOST_SCOPE_CALLER
    env = os.environ
    return CallerContext(
        dispatched=env.get(CC_SESSION_ENV) == "1",
        supervised=env.get(SUPERVISED_ENV) == "1",
        session_id=env.get(SESSION_ID_ENV),
        origin=env.get(SESSION_ORIGIN_ENV),
        transcript_id=env.get(TRANSCRIPT_ID_ENV),
        legacy_session_id=env.get(LEGACY_SESSION_ID_ENV),
        profile=env.get(PROFILE_ENV) or None,
        claude_code=env.get(CLAUDE_CODE_ENV),
        codex_thread=env.get(CODEX_THREAD_ENV),
    )


_CODEX_LABEL = "codex"
_CLIENT_SESSION_NAMES = {_CODEX_LABEL: "Codex session"}
# client_kind's label for each caller profile: owner-mode Codex's stdio label and
# the external-client profiles a stdio server or a shared-service credential sets.
_PROFILE_KINDS = {
    _CODEX_LABEL: _CODEX_LABEL,
    "interactive": _CODEX_LABEL,
    "validator": "codex-sealed",
    "external": "codex-external",
}
#: Every label client_kind gives a Codex client, so a session id can follow it.
_CODEX_KINDS = frozenset(kind for kind in _PROFILE_KINDS.values() if kind != "unknown")


def client_session_name() -> str:
    """What to call the caller's session in a message: "Claude Code session"
    unless the caller is a labelled client with its own name (owner-mode Codex).
    Messages that tell the reader to restart use it, so a Codex user is not told
    to restart a Claude Code session; Claude Code's wording stays unchanged.
    """
    return _CLIENT_SESSION_NAMES.get(caller_context().profile or "", "Claude Code session")


def client_kind(external_profile: str | None = None) -> str:
    """Which agent client made the current call, as a label for records.

    *external_profile* is the MCP external-client profile a stdio server put in
    force for the call, if any (``"validator"`` for the sealed Codex validator);
    a caller outside an MCP tool passes nothing. In order:

    - the profile, *external_profile* or else :attr:`CallerContext.profile`:
      ``"codex-sealed"`` (validator), ``"codex-external"`` (external), ``"codex"``
      (owner-mode Codex: the ``codex`` stdio label or the ``interactive``
      credential);
    - ``"codex"``: ``CODEX_THREAD_ID`` is set (a Codex command shell);
    - ``"claude"``: ``CLAUDECODE`` is set;
    - ``"unknown"``: none of these, or a profile this table does not know.

    The Codex thread id is checked before ``CLAUDECODE`` because Codex passes
    its parent's environment to its shells, so Codex started from a Claude
    Code shell carries both. Claude Code started from a Codex shell would carry
    both too and reads as ``codex``: a known limit of a label that only sees
    the environment.

    This is a label for records. Nothing may grant or refuse anything on it.
    """
    ctx = caller_context()
    profile = external_profile or ctx.profile
    if profile:
        return _PROFILE_KINDS.get(profile, "unknown")
    if ctx.codex_thread:
        return _CODEX_LABEL
    if ctx.claude_code:
        return "claude"
    return "unknown"


def record_session_id(kind: str) -> str | None:
    """The session to name beside the :func:`client_kind` label *kind* on a record.

    The id follows the label, so a record never pairs one client with another's
    session: a Codex kind gets the Codex thread id, ``claude`` the Claude Code
    transcript id, anything else ``None``. Codex started from a Claude Code shell
    inherits that shell's transcript id, which names the parent session, not the
    one acting; an MCP server Codex starts gets no session variable at all, so
    its records carry ``None``. Like the label, it decides nothing.
    """
    ctx = caller_context()
    if kind in _CODEX_KINDS:
        return ctx.codex_thread or None
    if kind == "claude":
        return ctx.transcript_id or None
    return None


def drop_client_label(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Remove the client label from a child's environment, in place.

    A process Genesis spawns is never the labelled client itself, so it must
    not inherit the label and have its calls recorded as that client's.
    """
    env.pop(PROFILE_ENV, None)
    return env


def dispatched_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of *base* (default: this process's environment) marked dispatched.

    For an agent CLI Genesis starts unattended, such as ``codex exec``. Without
    the marker the child reads as the owner's own interactive client, which is
    what an unmarked session means everywhere else. The supervised marker is
    dropped: nobody watches this child, even when its parent was an
    owner-attended conversation. So are the parent's session and transcript
    ids and its client label, which the child is not. The origin class is kept, so a child of an
    external-influenced session stays classified as one.
    """
    env = dict(os.environ if base is None else base)
    env[CC_SESSION_ENV] = "1"
    for name in (SUPERVISED_ENV, SESSION_ID_ENV, TRANSCRIPT_ID_ENV, LEGACY_SESSION_ID_ENV):
        env.pop(name, None)
    drop_client_label(env)
    return env


def caller_context() -> CallerContext:
    """The current caller: the request-scoped value if set, else the environment.

    In a shared service there is no environment to fall back on, so work that
    lost its request scope is the least privileged caller.
    """
    request = _REQUEST_CALLER.get()
    if request is not None:
        return request
    if _shared_service:
        frame = sys._getframe(1)
        site = (frame.f_code.co_filename, frame.f_lineno)
        if site not in _warned_sites:  # bounded by source lines, for a log nothing rotates
            _warned_sites.add(site)
            logger.warning("Caller identity read outside a request at %s:%d", *site)
        return _LOST_SCOPE_CALLER
    return caller_context_from_env()


@contextmanager
def request_caller(context: CallerContext) -> Iterator[CallerContext]:
    """Make *context* the caller for the enclosed block, and restore the previous one on any exit."""
    token = _REQUEST_CALLER.set(context)
    try:
        yield context
    finally:
        _REQUEST_CALLER.reset(token)
