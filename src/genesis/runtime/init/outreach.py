"""Init function: _init_outreach."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize outreach pipeline, scheduler, calibration, MCP wiring."""
    if rt._db is None:
        logger.warning("Outreach skipped — no DB")
        return

    try:
        from genesis.calibration.curves import CalibrationCurveComputer
        from genesis.calibration.logger import PredictionLogger
        from genesis.calibration.reconciler import PredictionReconciler
        from genesis.content.drafter import ContentDrafter
        from genesis.content.formatter import ContentFormatter
        from genesis.mcp.outreach_mcp import init_outreach_mcp
        from genesis.outreach.config import load_outreach_config
        from genesis.outreach.engagement import EngagementTracker
        from genesis.outreach.fresh_eyes import FreshEyesReview
        from genesis.outreach.governance import GovernanceGate
        from genesis.outreach.morning_report import MorningReportGenerator
        from genesis.outreach.pipeline import OutreachPipeline as _Pipeline
        from genesis.outreach.scheduler import OutreachScheduler as _Scheduler

        config = load_outreach_config()
        governance = GovernanceGate(config, rt._db)
        fresh_eyes = FreshEyesReview(rt._router) if rt._router else None
        drafter = ContentDrafter(rt._router)
        formatter = ContentFormatter()

        channels: dict = {}
        recipients: dict = {}
        for key, val in os.environ.items():
            if key.startswith("OUTREACH_RECIPIENT_") and val:
                channel_name = key[len("OUTREACH_RECIPIENT_"):].lower()
                recipients[channel_name] = val.strip()
        if "telegram" not in recipients:
            tg_users = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
            if tg_users:
                recipients["telegram"] = tg_users.split(",")[0].strip()

        rt._outreach_pipeline = _Pipeline(
            governance=governance,
            drafter=drafter,
            formatter=formatter,
            channels=channels,
            fresh_eyes=fresh_eyes,
            deferred_queue=rt._deferred_work_queue,
            db=rt._db,
            config=config,
            recipients=recipients,
        )

        if hasattr(rt, "_output_router") and rt._output_router is not None:
            rt._output_router.set_outreach_pipeline(rt._outreach_pipeline)

        engagement = EngagementTracker(rt._db)
        rt._engagement_tracker = engagement

        morning = MorningReportGenerator(
            rt._health_data, rt._db, drafter,
            event_bus=rt._event_bus,
        )

        rt._prediction_logger = PredictionLogger(rt._db)
        reconciler = PredictionReconciler(rt._db)
        curve_computer = CalibrationCurveComputer(rt._db)

        rt._outreach_scheduler = _Scheduler(
            rt._outreach_pipeline, morning, engagement, config, rt._db,
            reconciler=reconciler,
            curve_computer=curve_computer,
            event_bus=rt._event_bus,
        )

        init_outreach_mcp(
            pipeline=rt._outreach_pipeline,
            engagement=engagement,
            config=config,
            db=rt._db,
            activity_tracker=rt._activity_tracker,
        )

        from genesis.mcp.recon_mcp import init_recon_mcp

        init_recon_mcp(
            db=rt._db,
            router=rt._router,
            activity_tracker=rt._activity_tracker,
            pipeline=rt._outreach_pipeline,
            memory_store=rt._memory_store,
        )

        logger.info("Step 13: Outreach pipeline + scheduler initialized")

    except ImportError:
        logger.warning("genesis.outreach not available")
    except Exception:
        logger.exception("Failed to initialize outreach")
