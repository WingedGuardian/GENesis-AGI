"""Watchdog runner — systemd oneshot entry point.

Called by genesis-watchdog.timer every 60s.
Checks health, takes action, exits.
"""

from __future__ import annotations

import logging
import sys

from genesis.autonomy.types import WatchdogAction
from genesis.autonomy.watchdog import WatchdogChecker, restart_bridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    checker = WatchdogChecker.from_yaml()
    action = checker.check()

    if action is WatchdogAction.SKIP:
        logger.debug("Health OK — no action needed")
        return 0

    if action is WatchdogAction.BACKOFF:
        logger.info("In backoff period — skipping this cycle")
        return 0

    if action is WatchdogAction.NOTIFY:
        logger.error("Bridge health unknown — manual intervention may be needed")
        # Exit non-zero so systemd logs it, but don't attempt restart
        return 1

    if action is WatchdogAction.RESTART:
        return restart_bridge()

    # Note: AZ health check runs inside WatchdogChecker._check_az_health()
    # and restarts directly — it does not return RESTART_AZ via check().

    logger.warning("Unknown watchdog action: %s", action)
    return 1


if __name__ == "__main__":
    sys.exit(main())
