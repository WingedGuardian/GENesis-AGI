"""Keep Genesis-launched Claude processes out of an active deploy window."""

from __future__ import annotations

import asyncio
import os

from genesis.env import update_in_progress

DEFAULT_SPAWN_WAIT_S = 60.0
_POLL_INTERVAL_S = 0.5


def spawn_wait_timeout() -> float:
    """Return the bounded wait for a deploy-held Claude spawn."""
    raw = os.environ.get("GENESIS_DEPLOY_SPAWN_WAIT_S")
    if raw is None:
        return DEFAULT_SPAWN_WAIT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_SPAWN_WAIT_S


async def wait_for_deploy_clear(*, timeout_s: float | None = None) -> bool:
    """Wait for deploy signals to clear, returning false on timeout.

    The check is repeated immediately before the subprocess is created.  Callers
    may also check earlier to avoid claiming queued work during a deploy, but
    this final check is the race-closing chokepoint.
    """
    timeout = spawn_wait_timeout() if timeout_s is None else max(0.0, timeout_s)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while update_in_progress():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_POLL_INTERVAL_S, remaining))
    return True
