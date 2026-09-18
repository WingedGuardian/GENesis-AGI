"""Shared probe plumbing for collectors that shell out.

Extracted because the same three pieces were written twice, and the copies had
already diverged: only one of them carried the no-user-bus carve-out, and only
one had timeout/cancellation tests. A second copy of a rule is a second place
to forget it.

The distinction all of this exists to preserve: a command that is ABSENT, or a
manager that cannot exist on this box, is an ANSWER — record it. A command that
is present and then fails tells us nothing, and recording nothing as a fact is
what bills a spurious drift observation plus an LLM annotation regeneration,
once when the value "changes" and again when it comes back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class ProbeFailed(RuntimeError):
    """A command that EXISTS could not be run to completion."""


async def reap(proc: asyncio.subprocess.Process | None) -> None:
    """Kill and wait. A child we stop reading from is still ours to reap.

    Accepts None so callers never need a guard: the spawn itself can raise —
    and `TimeoutError` is an `OSError` subclass, so a spawn-time ETIMEDOUT
    lands in a `except TimeoutError` clause with no process to reap.
    """
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001 - reaping is best-effort, never fatal
        # Deliberately not raised: the kill above already fired, and the child
        # watcher reaps independently. Logged so a persistent failure is
        # findable rather than silent.
        logger.debug("infra_profile: could not reap %s", proc, exc_info=True)


def user_bus_present() -> bool:
    """Is there a user D-Bus session for `systemctl --user` to talk to?

    Structural, not transient: a box without one never grows a bus between
    refreshes, so its unanswerable probe is a standing property rather than a
    failure to report. Probing the socket beats matching systemctl's stderr
    text, which is not a stable interface.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        return False
    return Path(runtime_dir, "bus").exists()
