"""Init function: _init_campaigns."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize campaign runner and MCP tools.

    Requires: db, direct_session_runner (must init after those steps).
    """
    if rt._db is None:
        logger.warning("Campaigns skipped — no DB")
        return

    session_runner = getattr(rt, "_direct_session_runner", None)
    if session_runner is None:
        logger.warning("Campaigns skipped — no DirectSessionRunner")
        return

    try:
        from genesis.campaigns.runner import CampaignRunner
        from genesis.mcp.health.campaign_tools import init_campaign_tools

        runner = CampaignRunner(
            db=rt._db,
            session_runner=session_runner,
            idle_detector=getattr(rt, "_idle_detector", None),
        )
        rt._campaign_runner = runner
        await runner.start()

        init_campaign_tools(runner=runner, db=rt._db)

        logger.info("Campaigns subsystem initialized")

    except ImportError:
        logger.warning("genesis.campaigns not available")
    except Exception:
        logger.exception("Failed to initialize campaigns")
