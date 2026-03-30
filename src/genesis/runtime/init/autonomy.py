"""Init function: _init_autonomy."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def init(rt: GenesisRuntime) -> None:
    """Initialize autonomy: protection, state machine, classification, verification."""
    try:
        from genesis.autonomy.classification import ActionClassifier
        from genesis.autonomy.protection import ProtectedPathRegistry
        from genesis.autonomy.verification import TaskVerifier

        rt._protected_paths = ProtectedPathRegistry.from_yaml()
        logger.info("Protected paths registry loaded")

        rt._action_classifier = ActionClassifier()
        logger.info("Action classifier loaded")

        rt._task_verifier = TaskVerifier()

        from genesis.autonomy.verification import _code_task_validator

        rt._task_verifier.register_validator("code", _code_task_validator)
        logger.info("Task verifier initialized (code validator registered)")

        if rt._db is not None:
            from genesis.autonomy.state_machine import AutonomyManager

            rt._autonomy_manager = AutonomyManager(
                db=rt._db,
                event_bus=rt._event_bus,
            )
            logger.info("Autonomy manager created (state loading deferred to first use)")
        else:
            logger.warning("DB not available — autonomy state machine disabled")

        if rt._db is not None:
            from genesis.autonomy.approval import ApprovalManager

            rt._approval_manager = ApprovalManager(
                db=rt._db,
                event_bus=rt._event_bus,
                classifier=rt._action_classifier,
            )

            from genesis.util.tasks import tracked_task

            async def _poll_approval_timeouts() -> None:
                while True:
                    try:
                        await asyncio.sleep(60)
                        expired = await rt._approval_manager.expire_timed_out()
                        if expired:
                            logger.info("Expired %d approval requests", expired)
                        rt.record_job_success("approval_timeout_poll")
                    except asyncio.CancelledError:
                        break
                    except Exception as exc:
                        rt.record_job_failure("approval_timeout_poll", str(exc))
                        logger.error("Approval timeout polling failed", exc_info=True)

            rt._approval_timeout_task = tracked_task(
                _poll_approval_timeouts(), name="approval-timeout-poller"
            )
            logger.info("Approval manager + timeout polling started")

        if rt._protected_paths and rt._cc_invoker:
            rt._cc_invoker.set_protected_paths(rt._protected_paths)
            logger.info("ProtectedPathRegistry wired into CCInvoker")

        logger.info("Step 14: Autonomy subsystem initialized")

    except ImportError:
        logger.warning("genesis.autonomy not available")
    except Exception:
        logger.exception("Failed to initialize autonomy")
