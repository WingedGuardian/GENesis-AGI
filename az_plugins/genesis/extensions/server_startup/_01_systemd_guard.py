"""Systemd startup guard — warns if AZ is not running under systemd.

Prevents the stale-nohup-process debugging nightmare (2026-03-22 incident).
When AZ runs outside systemd, crashes leave orphan processes that are
invisible to `systemctl` and write to the same log with no PID distinction.

Escape hatch: set INVOCATION_ID=dev for manual debugging.
"""

import logging
import os

from python.helpers.extension import Extension

logger = logging.getLogger("genesis.systemd_guard")


class SystemdGuard(Extension):
    async def execute(self, **kwargs):
        invocation_id = os.environ.get("INVOCATION_ID")
        if invocation_id is not None:
            # Running under systemd (or manually set for debugging)
            return

        logger.warning(
            "AZ is NOT running under systemd. This is discouraged — "
            "crashes will leave orphan processes that are hard to debug. "
            "Use: systemctl --user start agent-zero.service  "
            "Or set INVOCATION_ID=dev to suppress this warning."
        )
