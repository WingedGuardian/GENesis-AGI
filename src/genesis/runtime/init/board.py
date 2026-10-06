"""Bootstrap step ``board``: wire the read-only board reconciler.

Its own step (rather than a job added inside ``learning.init``) so the
bootstrap manifest carries a ``board`` key, which is what lets the health MCP
tell a board that never started from one that died.

The job rides the learning scheduler. When that scheduler is missing or not
running (learning init skipped or failed part-way), this step RAISES, so the
manifest records ``failed: learning scheduler unavailable`` and names the
real cause rather than blaming the board.

CronTrigger, not IntervalTrigger: an interval resets on every restart, so a
frequently-restarting server would never fire it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


async def init_board(rt) -> None:
    from apscheduler.triggers.cron import CronTrigger

    from genesis.board import config as board_config
    from genesis.board import reconciler
    from genesis.env import user_timezone

    scheduler = getattr(rt, "_learning_scheduler", None)
    if scheduler is None or not getattr(scheduler, "running", False):
        raise RuntimeError("learning scheduler unavailable — learning init skipped or not started")

    async def _tick() -> None:
        await reconciler.run_tick(rt)

    scheduler.add_job(
        _tick,
        CronTrigger(minute="*/5", timezone=user_timezone()),
        id=reconciler.JOB_ID,
        max_instances=1,
        misfire_grace_time=300,
    )
    # A start pulse, so the first tick (up to five minutes away, plus a full
    # board read) never reads as "never started".
    await reconciler._pulse(
        rt,
        {
            "tick_at": datetime.now(UTC).isoformat(),
            "board_state": "starting",
            "mode": board_config.effective_mode(),
        },
    )
    rt._board_reconciler = reconciler.JOB_ID
    logger.info("board reconciler wired (every 5 min)")
