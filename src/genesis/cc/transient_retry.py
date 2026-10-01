"""Re-run a CC call that failed because the provider was overloaded (HTTP 529).

An overload is a capacity blip on the provider's side, usually gone within
minutes, so a background dispatch that hits one should wait and try again
rather than fail. This helper does exactly that and nothing more:

* It retries ONLY :class:`CCOverloadedError`. A genuine rate limit, a quota
  lockout, a timeout or any other failure is re-raised on the first attempt,
  so the existing park / failover / retry lanes keep sole ownership of those.
* It waits 30s, 120s, then 300s before the three retries (four calls at most,
  about 7.5 minutes of waiting), then re-raises the last overload for the
  caller's normal failure handling (which, for a rate-limit subclass, includes
  the durable park where the caller has one).
* It adds NO timeout. Each call keeps its own ``CCInvocation.timeout_s``.

Where it sits matters. Callers wrap only ``invoker.run`` — BELOW the
autonomous-CLI approval decision (``route()``) and the inbox drop claim — so a
retry re-runs the very dispatch that was already approved. It never asks the
approval gate again, never bypasses it and never approves anything itself.

Replay. A retry re-runs the session from the start, so an overload that
arrived after the session already made tool calls would repeat them (the same
risk every other lane that re-runs a CC session carries). Two cases are
therefore NOT retried: a CLI result reporting more than one turn (tool
round-trips already happened), and an overload carrying MCP evidence (a tool's
own backend answering 529, not the provider's capacity). Residual risk: an
overload reported with no turn count is assumed to be pre-work. The turn count
comes only from the CLI's result object on the LAST line of raw stdout, so if a
CLI build ever reports a mid-session 529 as plain text with no result object,
or prints something after the result line, that case would still be re-run.
Callers that keep their own retry budget use :func:`replay_unsafe` to decide
whether an exhausted overload should spend it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

from genesis.cc.exceptions import CCOverloadedError

if TYPE_CHECKING:
    from genesis.cc.types import CCInvocation, CCOutput

logger = logging.getLogger(__name__)

# Waits before retry 1, 2 and 3. Long enough for a capacity blip to clear,
# short enough that a background cycle is not held for long; past the last one
# the caller's own failure path (park / fail-open / retriable row) takes over.
OVERLOAD_RETRY_DELAYS_S: tuple[int, ...] = (30, 120, 300)

# Module-level so tests patch it instead of sleeping for real.
_sleep = asyncio.sleep


class _Runner(Protocol):
    async def run(self, invocation: CCInvocation) -> CCOutput: ...


def replay_unsafe(exc: CCOverloadedError) -> bool:
    """True when re-running the session could repeat work it already did.

    Two signals, either one enough:

    * the CLI result reports more than one turn — the session had already made
      tool round-trips before the overload, so a re-run replays them;
    * the error carries MCP evidence — a tool's own backend answering 529 inside
      the session, which is not the provider's capacity at all.

    An overload with no turn count (plain stderr text) is treated as a failure
    before any work, which is what the CLI reports for a first-turn 529.
    """
    from genesis.cc.peer_availability import mentions_mcp

    num_turns = getattr(exc, "num_turns", None)
    if isinstance(num_turns, int) and num_turns > 1:
        return True
    return mentions_mcp(exc)


async def run_with_overload_retry(
    invoker: _Runner,
    invocation: CCInvocation,
) -> CCOutput:
    """``invoker.run(invocation)``, re-run after a provider overload.

    The same (frozen) ``invocation`` is passed on every attempt. Anything other
    than a provider :class:`CCOverloadedError` propagates unchanged on the
    attempt that raised it.
    """
    attempt = 0
    while True:
        try:
            return await invoker.run(invocation)
        except CCOverloadedError as exc:
            if attempt >= len(OVERLOAD_RETRY_DELAYS_S) or replay_unsafe(exc):
                raise
            delay = OVERLOAD_RETRY_DELAYS_S[attempt]
            attempt += 1
            logger.warning(
                "CC provider overloaded (%s); retry %d/%d in %ds",
                invocation.caller_tag or "untagged caller",
                attempt,
                len(OVERLOAD_RETRY_DELAYS_S),
                delay,
            )
            await _sleep(delay)
