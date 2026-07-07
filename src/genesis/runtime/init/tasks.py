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
        from genesis.autonomy.executor.research import DeepResearcherImpl
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
        decomposer = TaskDecomposer(
            router=rt._router,
            invoker=rt._cc_invoker,
            db=rt._db,
            memory_store=getattr(rt, "_memory_store", None),
            retriever=getattr(rt, "_hybrid_retriever", None),
        )
        reviewer = TaskReviewer(
            router=rt._router,
            invoker=rt._cc_invoker,
        )
        workaround = WorkaroundSearcherImpl(db=rt._db)
        researcher = DeepResearcherImpl(
            db=rt._db,
            retriever=getattr(rt, "_hybrid_retriever", None),
            router=rt._router,
            invoker=rt._cc_invoker,
            event_bus=rt._event_bus,
            # GROUNDWORK(web-dd): web_searcher not yet on runtime; due diligence
            # degrades to memory-only until WebSearcher is wired as a service.
            web_searcher=getattr(rt, "_web_searcher", None),
        )
        tracer = ExecutionTracer(
            db=rt._db,
            memory_store=getattr(rt, "_memory_store", None),
            router=rt._router,
        )

        # Single-task execution: semaphore ensures only one task runs
        # at a time.  Shared between executor (pause releases it) and
        # dispatcher (guarded_execute acquires it).
        exec_semaphore = asyncio.Semaphore(1)

        executor = CCSessionExecutor(
            db=rt._db,
            invoker=rt._cc_invoker,
            decomposer=decomposer,
            reviewer=reviewer,
            workaround_searcher=workaround,
            research_searcher=researcher,
            router=rt._router,
            tracer=tracer,
            outreach_pipeline=getattr(rt, "_outreach_pipeline", None),
            event_bus=rt._event_bus,
            runtime=rt,
            autonomous_dispatcher=getattr(rt, "_autonomous_dispatcher", None),
            exec_semaphore=exec_semaphore,
        )

        dispatcher = TaskDispatcher(
            db=rt._db,
            executor=executor,
            event_bus=rt._event_bus,
            exec_semaphore=exec_semaphore,
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

        # Capability-build lane: consumes inbox `build` verdicts into one-tap
        # greenlight cards and drives approved builds to draft PRs via the
        # dispatcher. Ships dark (build_lane.enabled default OFF). Always
        # constructed so the monitor hook is a clean no-op when disabled; the
        # poll loop is spawned ONLY when enabled (no idle churn while dark).
        try:
            from genesis.autonomy.build_lane import BuildLane
            from genesis.env import build_lane_enabled

            gate = getattr(rt, "_autonomous_cli_approval_gate", None)
            bl_enabled = build_lane_enabled()
            if bl_enabled and gate is None:
                logger.warning(
                    "build_lane.enabled=true but the approval gate is "
                    "unavailable — lane forced OFF (greenlight cards require "
                    "the gate)",
                )
                bl_enabled = False

            build_lane = BuildLane(
                db=rt._db,
                dispatcher=dispatcher,
                approval_gate=gate,
                enabled=bl_enabled,
            )
            rt._build_lane = build_lane

            # Late-wire the monitor hook (inbox init ran before tasks init).
            if rt._inbox_monitor is not None and hasattr(
                rt._inbox_monitor, "set_build_lane",
            ):
                rt._inbox_monitor.set_build_lane(build_lane)

            if bl_enabled:
                async def _build_lane_poll_loop() -> None:
                    while True:
                        try:
                            await asyncio.sleep(90)
                            await build_lane.poll_pending()
                            rt.record_job_success("build_lane_poll")
                        except asyncio.CancelledError:
                            break
                        except Exception as exc:
                            rt.record_job_failure("build_lane_poll", str(exc))
                            logger.error(
                                "Build-lane polling failed", exc_info=True,
                            )

                rt._build_lane_poll = tracked_task(
                    _build_lane_poll_loop(), name="build-lane-poll",
                )
                logger.info("Capability-build lane ENABLED (poll loop active)")
            else:
                logger.info("Capability-build lane constructed (dark — disabled)")
        except ImportError:
            logger.warning("Build lane modules not available")
        except Exception:
            logger.exception("Failed to initialize build lane")

        logger.info("Task executor subsystem initialized")

    except ImportError:
        logger.warning("Task executor modules not available")
    except Exception:
        logger.exception("Failed to initialize task executor")
