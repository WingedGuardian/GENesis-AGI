"""Init function: _init_ego.

Two-ego architecture:
- **User Ego (CEO)**: Proactive user value. Opus, user-focused context.
  Owns morning reports. Single voice to the user.
- **Genesis Ego (COO)**: Self-maintenance. Sonnet, system-focused context.
  No morning reports. Escalates to user ego via observations.

Both share the same EgoSession infrastructure — they differ in context
builder, prompt, model, cadence, and state keys.

TopicManager and ReplyWaiter are NOT available at bootstrap time —
they're created in standalone.py after Telegram init. ProposalWorkflow
is initialized without them; standalone.py wires them via setters later.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")

_IDENTITY_DIR = Path(__file__).resolve().parents[2] / "identity"


async def init(rt: GenesisRuntime) -> None:
    """Initialize both egos: user ego + genesis ego."""
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
        from genesis.ego.dispatch import EgoDispatcher
        from genesis.ego.genesis_context import GenesisEgoContextBuilder
        from genesis.ego.proposals import ProposalWorkflow
        from genesis.ego.session import EgoSession
        from genesis.ego.user_context import UserEgoContextBuilder
        from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS

        config = load_ego_config()
        if not config.enabled:
            logger.info("Ego disabled by config — skipping")
            return

        # MCP config: reflection profile (genesis-health + genesis-memory)
        config_builder = SessionConfigBuilder()
        mcp_config_path = config_builder.build_mcp_config("reflection")

        # -- Shared components --
        # ProposalWorkflow is shared — both egos create proposals that go
        # through the same Telegram approval pipeline.
        proposal_workflow = ProposalWorkflow(
            db=rt._db,
            memory_store=rt._memory_store,
        )
        rt._ego_proposal_workflow = proposal_workflow

        dispatcher = EgoDispatcher(db=rt._db)

        # ================================================================
        # USER EGO (CEO) — proactive user value
        # ================================================================
        user_ego_compaction = CompactionEngine(
            db=rt._db,
            router=rt._router,
            state_key_summary="user_ego_compacted_summary",
            focus_summary_key="ego_focus_summary",
            call_site_id="8_user_ego_compaction",
        )

        user_ego_context = UserEgoContextBuilder(
            db=rt._db,
            health_data=rt._health_data,
            capabilities=_CAPABILITY_DESCRIPTIONS,
        )

        user_ego_invoker = CCInvoker()

        user_ego_session = EgoSession(
            invoker=user_ego_invoker,
            session_manager=rt._session_manager,
            compaction_engine=user_ego_compaction,
            context_builder=user_ego_context,
            proposal_workflow=proposal_workflow,
            dispatcher=dispatcher,
            config=config,  # Uses base config (Opus, HIGH effort)
            db=rt._db,
            event_bus=rt._event_bus,
            direct_session_runner=rt._direct_session_runner,
            mcp_config_path=mcp_config_path,
            prompt_path=_IDENTITY_DIR / "USER_EGO_SESSION.md",
            call_site="7_user_ego_cycle",
            session_id_key="user_ego_cc_session_id",
            focus_summary_key="ego_focus_summary",  # User ego owns the canonical focus
            source_tag="user_ego_cycle",
        )

        if rt._autonomous_dispatcher is not None:
            user_ego_session.set_autonomous_dispatcher(rt._autonomous_dispatcher)

        # Store as the primary ego session (backwards compatible)
        rt._ego_session = user_ego_session

        user_ego_cadence = EgoCadenceManager(
            session=user_ego_session,
            config=config,
            idle_detector=rt._idle_detector,
            db=rt._db,
            event_bus=rt._event_bus,
        )
        rt._ego_cadence_manager = user_ego_cadence

        await user_ego_cadence.start()
        logger.info(
            "User ego initialized (cadence=%dm, model=%s, effort=%s)",
            config.cadence_minutes,
            config.model,
            config.default_effort,
        )

        # ================================================================
        # GENESIS EGO (COO) — self-maintenance
        # ================================================================
        genesis_ego_config = dataclasses.replace(
            config,
            model="sonnet",
            default_effort="high",
            cadence_minutes=max(config.cadence_minutes, 60),
            morning_report_enabled=False,
            ego_thinking_budget_usd=2.0,
            ego_dispatch_budget_usd=1.0,
        )

        genesis_ego_compaction = CompactionEngine(
            db=rt._db,
            router=rt._router,
            state_key_summary="genesis_ego_compacted_summary",
            focus_summary_key="genesis_ego_focus_summary",
            call_site_id="8_genesis_ego_compaction",
        )

        genesis_ego_context = GenesisEgoContextBuilder(
            db=rt._db,
            health_data=rt._health_data,
            capabilities=_CAPABILITY_DESCRIPTIONS,
        )

        genesis_ego_invoker = CCInvoker()

        genesis_ego_session = EgoSession(
            invoker=genesis_ego_invoker,
            session_manager=rt._session_manager,
            compaction_engine=genesis_ego_compaction,
            context_builder=genesis_ego_context,
            proposal_workflow=proposal_workflow,
            dispatcher=dispatcher,
            config=genesis_ego_config,
            db=rt._db,
            event_bus=rt._event_bus,
            direct_session_runner=rt._direct_session_runner,
            mcp_config_path=mcp_config_path,
            prompt_path=_IDENTITY_DIR / "GENESIS_EGO_SESSION.md",
            call_site="7_genesis_ego_cycle",
            session_id_key="genesis_ego_cc_session_id",
            focus_summary_key="genesis_ego_focus_summary",  # Separate from user ego
            source_tag="genesis_ego_cycle",
        )

        if rt._autonomous_dispatcher is not None:
            genesis_ego_session.set_autonomous_dispatcher(rt._autonomous_dispatcher)

        rt._genesis_ego_session = genesis_ego_session

        genesis_ego_cadence = EgoCadenceManager(
            session=genesis_ego_session,
            config=genesis_ego_config,
            idle_detector=rt._idle_detector,
            db=rt._db,
            event_bus=rt._event_bus,
        )
        rt._genesis_ego_cadence_manager = genesis_ego_cadence

        await genesis_ego_cadence.start()
        logger.info(
            "Genesis ego initialized (cadence=%dm, model=%s, effort=%s)",
            genesis_ego_config.cadence_minutes,
            genesis_ego_config.model,
            genesis_ego_config.default_effort,
        )

        # ================================================================
        # OUTCOME TRACKING — on_end hook for ego-dispatched sessions
        # ================================================================
        if rt._session_manager is not None:
            from genesis.cc.session_manager import cc_sessions
            from genesis.db.crud import observations as obs_crud

            async def _ego_dispatch_on_end(session_id: str) -> None:
                """Create an observation when an ego-dispatched session ends."""
                try:
                    session = await cc_sessions.get_by_id(rt._db, session_id)
                    if not session:
                        return
                    if session.get("source_tag") != "ego_dispatch":
                        return

                    import uuid
                    from datetime import UTC, datetime

                    status = session.get("status", "unknown")
                    cost = session.get("cost_usd", 0.0)
                    caller_ctx = session.get("caller_context", "")

                    await obs_crud.create(
                        rt._db,
                        id=str(uuid.uuid4()),
                        source="ego_dispatch",
                        type="execution_outcome",
                        content=(
                            f"Ego dispatch session {session_id[:8]} "
                            f"completed: status={status}, cost=${cost:.4f}. "
                            f"Context: {caller_ctx}"
                        ),
                        priority="medium",
                        category="ego_dispatch",
                        created_at=datetime.now(UTC).isoformat(),
                    )
                    logger.info(
                        "Recorded ego dispatch outcome for session %s",
                        session_id[:8],
                    )
                except Exception:
                    logger.warning(
                        "Failed to record ego dispatch outcome",
                        exc_info=True,
                    )

            rt._session_manager.add_on_end(_ego_dispatch_on_end)
            logger.info("Ego dispatch outcome hook registered")

    except ImportError:
        logger.warning("genesis.ego not available")
    except Exception:
        logger.exception("Failed to initialize ego")
