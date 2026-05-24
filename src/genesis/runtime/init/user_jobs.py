"""Init function: _init_user_jobs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize user job scheduler and wire MCP tools."""
    if rt._db is None:
        logger.warning("User jobs skipped — no DB")
        return

    try:
        from genesis.mcp.health.user_job_tools import init_user_job_tools
        from genesis.scheduler.user_jobs import UserJobScheduler

        scheduler = UserJobScheduler(db=rt._db, event_bus=rt._event_bus)
        rt._user_job_scheduler = scheduler

        await scheduler.start()

        init_user_job_tools(db=rt._db, scheduler=scheduler)

        logger.info("Step 14: User job scheduler initialized")

    except ImportError:
        logger.warning("genesis.scheduler.user_jobs not available")
    except Exception:
        logger.exception("Failed to initialize user jobs")
