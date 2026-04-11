"""Init function: _init_cc_relay."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize CC relay: invoker, session manager, checkpoints, reflection bridge."""
    if rt._db is None:
        logger.warning("DB not available — skipping CC relay")
        return
    try:
        from genesis.cc.checkpoint import CheckpointManager
        from genesis.cc.invoker import CCInvoker
        from genesis.cc.reflection_bridge import CCReflectionBridge
        from genesis.cc.session_manager import SessionManager

        async def _on_cc_status_change(status_str: str) -> None:
            sm = rt._resilience_state_machine
            if sm is not None:
                from genesis.resilience.state import CCStatus
                status_map = {
                    "NORMAL": CCStatus.NORMAL,
                    "RATE_LIMITED": CCStatus.RATE_LIMITED,
                    "UNAVAILABLE": CCStatus.UNAVAILABLE,
                }
                cc_status = status_map.get(status_str)
                if cc_status is not None:
                    sm.update_cc(cc_status)
                    logger.info("CC status updated to %s", status_str)

        async def _on_model_downgrade(requested: str, actual: str, session_id: str) -> None:
            """Record CC model downgrade as observation + event."""
            logger.warning(
                "CC model downgrade: requested=%s actual=%s session=%s",
                requested, actual, session_id,
            )
            if rt._event_bus:
                try:
                    from genesis.observability.types import Severity, Subsystem
                    await rt._event_bus.emit(
                        Subsystem.PROVIDERS,
                        Severity.WARNING,
                        "cc.model_downgrade",
                        f"CC downgraded {requested}->{actual}",
                        requested_model=requested,
                        actual_model=actual,
                        session_id=session_id,
                    )
                except Exception:
                    logger.warning("Failed to emit model downgrade event", exc_info=True)
            if rt._db:
                try:
                    import uuid
                    from datetime import UTC, datetime

                    from genesis.db.crud import observations
                    content = f"model_downgrade:{requested}->{actual}"
                    await observations.create(
                        rt._db,
                        id=str(uuid.uuid4()),
                        source="cc_invoker",
                        type="model_downgrade",
                        content=content,
                        priority="high",
                        created_at=datetime.now(UTC).isoformat(),
                        skip_if_duplicate=True,
                    )
                except Exception:
                    logger.error("Failed to create model downgrade observation", exc_info=True)

        rt._cc_invoker = CCInvoker(
            on_cc_status_change=_on_cc_status_change,
            on_model_downgrade=_on_model_downgrade,
        )
        rt._session_manager = SessionManager(
            db=rt._db,
            invoker=rt._cc_invoker,
            event_bus=rt._event_bus,
        )
        rt._checkpoint_manager = CheckpointManager(
            db=rt._db,
            session_manager=rt._session_manager,
            invoker=rt._cc_invoker,
            event_bus=rt._event_bus,
        )
        rt._cc_reflection_bridge = CCReflectionBridge(
            session_manager=rt._session_manager,
            invoker=rt._cc_invoker,
            db=rt._db,
            event_bus=rt._event_bus,
        )

        from genesis.resilience.cc_budget import CCBudgetTracker

        rt._cc_budget_tracker = CCBudgetTracker(db=rt._db)

        try:
            from genesis.routing.model_profiles import ModelProfileRegistry

            profiles_path = Path(__file__).resolve().parents[3] / "config" / "model_profiles.yaml"
            rt._model_profile_registry = ModelProfileRegistry(profiles_path)
            rt._model_profile_registry.load()
            logger.info("Model profile registry loaded (%d profiles)", len(rt._model_profile_registry.all_profiles()))
        except Exception as exc:
            rt._model_profile_registry = None
            logger.warning("Failed to load model profile registry", exc_info=True)
            from genesis.runtime._degradation import record_init_degradation
            await record_init_degradation(rt._db, rt._event_bus, "cc_relay", "model_profile_registry", str(exc))

        if rt._router is not None:
            try:
                from genesis.cc.contingency import CCContingencyDispatcher

                rt._contingency_dispatcher = CCContingencyDispatcher(
                    router=rt._router,
                    profile_registry=rt._model_profile_registry,
                    deferred_queue=rt._deferred_work_queue,
                )
                logger.info("CC contingency dispatcher initialized")
            except Exception as exc:
                rt._contingency_dispatcher = None
                logger.warning("Failed to initialize CC contingency dispatcher", exc_info=True)
                from genesis.runtime._degradation import record_init_degradation
                await record_init_degradation(rt._db, rt._event_bus, "cc_relay", "contingency_dispatcher", str(exc))
        else:
            logger.warning("Router unavailable — CC contingency dispatcher disabled")

        if (
            rt._awareness_loop is not None
            and hasattr(rt._awareness_loop, "set_cc_reflection_bridge")
        ):
            rt._awareness_loop.set_cc_reflection_bridge(
                rt._cc_reflection_bridge
            )
            logger.info("CC reflection bridge injected into awareness loop")

        logger.info("Genesis CC relay initialized")
    except ImportError:
        logger.warning("genesis.cc not available")
    except Exception:
        logger.exception("Failed to initialize CC relay")
