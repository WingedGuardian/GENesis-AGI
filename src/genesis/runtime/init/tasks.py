"""Init function: _init_tasks — autonomous task executor subsystem."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize task executor: decomposer, reviewer, executor, dispatcher.

    Wires the full autonomous task pipeline and runs crash recovery.
    Registers a background polling loop for observation-based task dispatch.
    """
    try:
        from genesis.autonomy.decomposer import TaskDecomposer
        from genesis.autonomy.dispatcher import TaskDispatcher
        from genesis.autonomy.executor.engine import CCSessionExecutor
        from genesis.autonomy.executor.review import TaskReviewer
        from genesis.autonomy.executor.trace import ExecutionTracer
        from genesis.autonomy.executor.workaround import WorkaroundSearcherImpl

        if rt._db is None:
            logger.warning("DB not available — task executor disabled")
            return

        if rt._router is None:
            logger.warning("Router not available — task executor disabled")
            return

        # Build components
        decomposer = TaskDecomposer(router=rt._router)
        reviewer = TaskReviewer(router=rt._router)
        workaround = WorkaroundSearcherImpl(db=rt._db)
        tracer = ExecutionTracer(
            db=rt._db,
            memory_store=getattr(rt, "_memory_store", None),
        )

        executor = CCSessionExecutor(
            db=rt._db,
            invoker=rt._cc_invoker,
            decomposer=decomposer,
            reviewer=reviewer,
            workaround_searcher=workaround,
            tracer=tracer,
            outreach_pipeline=getattr(rt, "_outreach_pipeline", None),
            event_bus=rt._event_bus,
            runtime=rt,
            autonomous_dispatcher=getattr(rt, "_autonomous_dispatcher", None),
        )

        dispatcher = TaskDispatcher(
            db=rt._db,
            executor=executor,
            event_bus=rt._event_bus,
        )

        rt._task_executor = executor
        rt._task_dispatcher = dispatcher

        # Wire MCP tools
        try:
            from genesis.mcp.health.task_tools import init_task_tools
            init_task_tools(dispatcher, executor, db=rt._db)
        except ImportError:
            logger.warning("Task MCP tools not available")

        # Crash recovery
        try:
            recovered = await dispatcher.recover_incomplete()
            if recovered:
                logger.info("Recovered %d incomplete tasks", recovered)
        except Exception:
            logger.error("Task crash recovery failed", exc_info=True)

        # Background polling loop for observation-based dispatch
        from genesis.util.tasks import tracked_task

        async def _dispatch_poll_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(120)  # 2-minute interval
                    count = await dispatcher.dispatch_cycle()
                    if count:
                        logger.info("Dispatch cycle: %d tasks dispatched", count)
                    rt.record_job_success("task_dispatch_poll")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    rt.record_job_failure("task_dispatch_poll", str(exc))
                    logger.error("Task dispatch polling failed", exc_info=True)

        rt._task_dispatch_poll = tracked_task(
            _dispatch_poll_loop(), name="task-dispatch-poll",
        )

        logger.info("Task executor subsystem initialized")

    except ImportError:
        logger.warning("Task executor modules not available")
    except Exception:
        logger.exception("Failed to initialize task executor")
