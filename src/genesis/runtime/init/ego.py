"""Init function: _init_ego.

Wires the ego subsystem — Genesis's autonomous decision-making cycle.
Creates EgoSession (orchestrator) and EgoCadenceManager (scheduler),
starts the cadence manager so ego cycles begin firing.

TopicManager and ReplyWaiter are NOT available at bootstrap time —
they're created in standalone.py after Telegram init. ProposalWorkflow
is initialized without them; standalone.py wires them via setters later.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize ego: session orchestrator + cadence manager."""
    # Hard dependencies — skip if unavailable
    if rt._db is None:
        logger.warning("DB not available — ego disabled")
        return
    if rt._router is None:
        logger.warning("Router not available — ego disabled")
        return
    if rt._cc_invoker is None or rt._session_manager is None:
        logger.warning("CC relay not available — ego disabled")
        return

    try:
        from genesis.cc.invoker import CCInvoker
        from genesis.cc.session_config import SessionConfigBuilder
        from genesis.ego.cadence import EgoCadenceManager
        from genesis.ego.compaction import CompactionEngine
        from genesis.ego.config import load_ego_config
        from genesis.ego.context import EgoContextBuilder
        from genesis.ego.dispatch import EgoDispatcher
        from genesis.ego.proposals import ProposalWorkflow
        from genesis.ego.session import EgoSession
        from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS

        config = load_ego_config()
        if not config.enabled:
            logger.info("Ego disabled by config — skipping")
            return

        compaction = CompactionEngine(db=rt._db, router=rt._router)

        context_builder = EgoContextBuilder(
            db=rt._db,
            health_data=rt._health_data,
            capabilities=_CAPABILITY_DESCRIPTIONS,
        )

        # TopicManager + ReplyWaiter wired later in standalone.py
        proposal_workflow = ProposalWorkflow(db=rt._db)
        rt._ego_proposal_workflow = proposal_workflow

        dispatcher = EgoDispatcher(db=rt._db)

        # Dedicated invoker for the ego — prevents _active_proc race
        # conditions with other concurrent CC invocations (same pattern
        # as DirectSessionRunner at runtime/init/direct_session.py).
        ego_invoker = CCInvoker()

        # MCP config: reflection profile (genesis-health + genesis-memory)
        config_builder = SessionConfigBuilder()
        mcp_config_path = config_builder.build_mcp_config("reflection")

        session = EgoSession(
            invoker=ego_invoker,
            session_manager=rt._session_manager,
            compaction_engine=compaction,
            context_builder=context_builder,
            proposal_workflow=proposal_workflow,
            dispatcher=dispatcher,
            config=config,
            db=rt._db,
            event_bus=rt._event_bus,
            mcp_config_path=mcp_config_path,
        )

        # Wire autonomous dispatcher if available (for approval gating)
        if rt._autonomous_dispatcher is not None:
            session.set_autonomous_dispatcher(rt._autonomous_dispatcher)

        rt._ego_session = session

        cadence = EgoCadenceManager(
            session=session,
            config=config,
            idle_detector=rt._idle_detector,  # from surplus init, may be None
            db=rt._db,
            event_bus=rt._event_bus,
        )
        rt._ego_cadence_manager = cadence

        await cadence.start()
        logger.info(
            "Ego initialized (cadence=%dm, model=%s, effort=%s, budget=$%.2f/day)",
            config.cadence_minutes,
            config.model,
            config.default_effort,
            config.daily_budget_cap_usd,
        )

    except ImportError:
        logger.warning("genesis.ego not available")
    except Exception:
        logger.exception("Failed to initialize ego")
