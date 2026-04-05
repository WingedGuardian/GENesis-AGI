"""Task executor engine -- state machine for autonomous task execution.

Drives a task through its lifecycle:
    PENDING -> REVIEWING -> PLANNING -> EXECUTING -> VERIFYING
    -> SYNTHESIZING -> DELIVERING -> RETROSPECTIVE -> COMPLETED

Implements:
- Amendment #1:  Blocker persistence (DB before notification)
- Amendment #4:  Review iteration cap (max 2)
- Amendment #7:  Worktree management for CODE steps
- Amendment #10: Formal state transitions via validate_transition()
- Amendment #13: Global pause check at each checkpoint
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchRequest
from genesis.autonomy.executor import dispatch as _dispatch
from genesis.autonomy.executor import worktree_mgr as _worktree
from genesis.autonomy.executor.types import (
    ExecutionTrace,
    InvalidTransitionError,
    StepResult,
    StepType,
    TaskPhase,
    validate_transition,
)

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 2  # Amendment #4

# Repo root: four parents up from executor/engine.py -> src/genesis/autonomy/executor
_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKTREE_BASE = _REPO_ROOT / ".claude" / "worktrees"


class CCSessionExecutor:
    """State machine driving autonomous multi-step task execution."""

    def __init__(
        self,
        *,
        db: Any,
        invoker: Any,
        decomposer: Any,
        reviewer: Any,
        workaround_searcher: Any | None = None,
        tracer: Any | None = None,
        outreach_pipeline: Any | None = None,
        event_bus: Any | None = None,
        runtime: Any | None = None,
        autonomous_dispatcher: Any | None = None,
    ) -> None:
        self._db = db
        self._invoker = invoker
        self._decomposer = decomposer
        self._reviewer = reviewer
        self._workaround = workaround_searcher
        self._tracer = tracer
        self._outreach = outreach_pipeline
        self._event_bus = event_bus
        self._runtime = runtime
        self._autonomous_dispatcher = autonomous_dispatcher

        # In-memory state
        self._active_tasks: dict[str, TaskPhase] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._paused_tasks: set[str] = set()
        self._worktree_paths: dict[str, Path] = {}

    # =================================================================
    # Main lifecycle
    # =================================================================

    async def execute(self, task_id: str) -> bool:
        """Run a task through its full lifecycle.

        Returns ``True`` on successful completion, ``False`` on
        blocker/failure/cancellation.
        """
        from genesis.db.crud import task_states, task_steps

        task = await task_states.get_by_id(self._db, task_id)
        if task is None:
            logger.error("Task %s not found in DB", task_id)
            return False

        plan_path = task.get("outputs") or ""
        if not plan_path:
            logger.error("Task %s has no plan path in outputs column", task_id)
            await self._fail_task(task_id, "No plan path specified")
            return False

        try:
            plan_content = Path(plan_path).read_text(encoding="utf-8")
        except OSError:
            logger.error("Cannot read plan at %s", plan_path, exc_info=True)
            await self._fail_task(task_id, f"Cannot read plan: {plan_path}")
            return False

        description = task.get("description", "")

        # Register task
        self._active_tasks[task_id] = TaskPhase.PENDING
        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event

        # Start trace
        trace: ExecutionTrace | None = None
        if self._tracer:
            trace = self._tracer.start_trace(task_id, "user", description)

        try:
            # --- REVIEWING ---
            await self._transition(task_id, TaskPhase.REVIEWING)
            review = await self._reviewer.review_plan(plan_content, description)

            if not review.passed:
                gap_text = "; ".join(review.gaps) if review.gaps else "unspecified"
                await self._persist_blocker(
                    task_id,
                    f"Plan review found gaps: {gap_text}",
                    TaskPhase.REVIEWING,
                )
                return False

            # --- PLANNING ---
            await self._transition(task_id, TaskPhase.PLANNING)
            steps = await self._decomposer.decompose(plan_content, description)

            for step in steps:
                await task_steps.create_step(
                    self._db,
                    task_id=task_id,
                    step_idx=step["idx"],
                    step_type=step.get("type", "code"),
                    description=step.get("description", ""),
                )

            # Create worktree if any step is CODE (Amendment #7)
            has_code = any(s.get("type") == "code" for s in steps)
            if has_code:
                await self._create_worktree(task_id)

            await self._notify(
                task_id,
                f"Proceeding with task: {description} "
                f"({len(steps)} steps)",
                "alert",
            )

            # --- EXECUTING ---
            await self._transition(task_id, TaskPhase.EXECUTING)
            step_results: list[StepResult] = []

            for step in steps:
                # Check cancel before each step
                if cancel_event.is_set():
                    await self._transition(task_id, TaskPhase.CANCELLED)
                    return False

                result = await self._execute_step(
                    task_id, step, step_results,
                )
                step_results.append(result)

                if trace:
                    self._tracer.record_step(trace, result)

                # Checkpoint: save + check pause/cancel
                should_continue = await self._checkpoint(
                    task_id, step["idx"], result,
                )
                if not should_continue:
                    return False

                # Handle blocked step
                if result.status == "blocked":
                    await self._persist_blocker(
                        task_id,
                        result.blocker_description or "Step blocked",
                        TaskPhase.EXECUTING,
                        step_idx=step["idx"],
                    )
                    return False

                # Handle failed step -- try workaround
                if result.status == "failed":
                    recovered = await self._try_workaround(
                        task_id, step, result, step_results,
                    )
                    if recovered is not None:
                        step_results[-1] = recovered
                        if trace:
                            self._tracer.record_step(trace, recovered)
                    else:
                        await self._fail_task(
                            task_id,
                            f"Step {step['idx']} failed: {result.result[:300]}",
                        )
                        return False

            # --- VERIFYING (review loop, Amendment #4) ---
            deliverable = _dispatch.synthesize_deliverable(step_results)

            review_passed = False
            last_verify = None
            for iteration in range(MAX_REVIEW_ITERATIONS):
                await self._transition(task_id, TaskPhase.VERIFYING)
                verify = await self._reviewer.verify_deliverable(
                    deliverable,
                    plan_content,
                    task_type=_dispatch.dominant_step_type(steps),
                    iteration=iteration,
                )
                last_verify = verify

                if trace:
                    self._tracer.record_quality_gate(trace, {
                        "iteration": iteration,
                        "passed": verify.passed,
                        "skipped": verify.skipped_reason,
                        "issues": verify.programmatic_issues,
                    })

                if verify.passed:
                    review_passed = True
                    break

                # Not last iteration -- create fixup step and re-run
                if iteration < MAX_REVIEW_ITERATIONS - 1:
                    await self._transition(task_id, TaskPhase.EXECUTING)
                    fixup = _dispatch.create_fixup_step(
                        verify, len(steps) + iteration,
                    )
                    fixup_result = await self._execute_step(
                        task_id, fixup, step_results,
                    )
                    step_results.append(fixup_result)
                    deliverable = _dispatch.synthesize_deliverable(step_results)

            if not review_passed:
                # Amendment #4: escalate after cap
                feedback_parts = []
                if last_verify and last_verify.fresh_eyes_feedback:
                    feedback_parts.append(
                        f"Fresh eyes: {last_verify.fresh_eyes_feedback[:500]}"
                    )
                if last_verify and last_verify.adversarial_feedback:
                    feedback_parts.append(
                        f"Adversarial: {last_verify.adversarial_feedback[:500]}"
                    )
                escalation = (
                    f"Review failed after {MAX_REVIEW_ITERATIONS} iterations.\n"
                    + "\n".join(feedback_parts)
                )
                await self._persist_blocker(
                    task_id, escalation, TaskPhase.VERIFYING,
                )
                return False

            # --- SYNTHESIZING ---
            await self._transition(task_id, TaskPhase.SYNTHESIZING)
            await self._set_output(task_id, "deliverable", deliverable)

            # --- DELIVERING ---
            await self._transition(task_id, TaskPhase.DELIVERING)
            await self._deliver(task_id, deliverable, steps)

            # --- RETROSPECTIVE ---
            await self._transition(task_id, TaskPhase.RETROSPECTIVE)
            if trace and self._tracer:
                try:
                    retro_id = await self._tracer.finalize(trace)
                    if retro_id:
                        await self._set_output(task_id, "retrospective_id", retro_id)
                except Exception:
                    logger.error(
                        "Retrospective failed for task %s (non-blocking)",
                        task_id, exc_info=True,
                    )

            # --- COMPLETED ---
            await self._transition(task_id, TaskPhase.COMPLETED)
            await self._notify(
                task_id, f"Task completed: {description}", "alert",
            )
            return True

        except asyncio.CancelledError:
            with contextlib.suppress(InvalidTransitionError):
                await self._transition(task_id, TaskPhase.CANCELLED)
            return False
        except InvalidTransitionError:
            logger.error(
                "Task %s hit invalid state transition (internal bug)",
                task_id, exc_info=True,
            )
            return False
        except Exception:
            logger.error(
                "Task %s failed with unhandled exception",
                task_id, exc_info=True,
            )
            with contextlib.suppress(InvalidTransitionError):
                await self._fail_task(task_id, "Unexpected error")
            return False
        finally:
            await self._cleanup_worktree(task_id)
            self._active_tasks.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
            self._pause_events.pop(task_id, None)

    # =================================================================
    # State machine (Amendment #10)
    # =================================================================

    async def _transition(self, task_id: str, to_phase: TaskPhase) -> None:
        """Validate and apply a state transition. Emits event."""
        from genesis.db.crud import task_states

        current = self._active_tasks.get(task_id, TaskPhase.PENDING)
        validate_transition(current, to_phase)

        self._active_tasks[task_id] = to_phase
        await task_states.update(
            self._db, task_id, current_phase=to_phase.value,
        )

        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.AUTONOMY,
                Severity.INFO,
                "task.phase_changed",
                f"Task {task_id[:8]}: {current.value} -> {to_phase.value}",
                task_id=task_id,
                from_phase=current.value,
                to_phase=to_phase.value,
            )

        logger.info("Task %s: %s -> %s", task_id, current.value, to_phase.value)

    async def _fail_task(self, task_id: str, reason: str) -> None:
        """Transition to FAILED and notify."""
        current = self._active_tasks.get(task_id, TaskPhase.PENDING)
        if current in (TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CANCELLED):
            return

        with contextlib.suppress(InvalidTransitionError):
            await self._transition(task_id, TaskPhase.FAILED)

        from genesis.db.crud import task_states
        await task_states.update(
            self._db, task_id, blockers=reason,
        )
        await self._notify(task_id, f"Task failed: {reason}", "alert")

    # =================================================================
    # Blocker persistence (Amendment #1)
    # =================================================================

    async def _persist_blocker(
        self,
        task_id: str,
        description: str,
        resume_phase: TaskPhase,
        *,
        step_idx: int | None = None,
    ) -> None:
        """Persist blocked state to DB BEFORE sending notification.

        This ordering is critical: if the process crashes between
        DB write and notification, recovery can re-send the notification.
        The reverse (notify then crash before DB write) would lose
        the blocker state entirely.
        """
        from genesis.db.crud import task_states

        blocker_json = json.dumps({
            "description": description,
            "resume_phase": resume_phase.value,
            "step_idx": step_idx,
            "blocked_at": datetime.now(UTC).isoformat(),
        })

        # 1. Persist to DB first
        await task_states.update(self._db, task_id, blockers=blocker_json)

        # 2. Transition to BLOCKED
        with contextlib.suppress(InvalidTransitionError):
            await self._transition(task_id, TaskPhase.BLOCKED)

        # 3. THEN notify
        await self._notify(task_id, f"Blocked: {description}", "blocker")

    # =================================================================
    # Checkpoint (Amendment #13: global pause)
    # =================================================================

    async def _checkpoint(
        self,
        task_id: str,
        step_idx: int,
        result: StepResult,
    ) -> bool:
        """Save progress and check for cancel/pause.

        Returns ``True`` to continue, ``False`` to stop.
        """
        # Check cancellation
        cancel = self._cancel_events.get(task_id)
        if cancel and cancel.is_set():
            await self._transition(task_id, TaskPhase.CANCELLED)
            return False

        # Amendment #13: global pause check + per-task pause
        should_pause = (
            (self._runtime and getattr(self._runtime, "paused", False))
            or task_id in self._paused_tasks
        )
        if should_pause:
            await self._transition(task_id, TaskPhase.PAUSED)
            pause_event = asyncio.Event()
            self._pause_events[task_id] = pause_event

            # Wait for resume OR cancel
            while not pause_event.is_set():
                if cancel and cancel.is_set():
                    with contextlib.suppress(InvalidTransitionError):
                        await self._transition(task_id, TaskPhase.CANCELLED)
                    return False
                await asyncio.sleep(0.1)

            self._paused_tasks.discard(task_id)
            await self._transition(task_id, TaskPhase.EXECUTING)

        # Emit progress event
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.AUTONOMY,
                Severity.INFO,
                "task.step_completed",
                f"Task {task_id[:8]} step {step_idx}: {result.status}",
                task_id=task_id,
                step_idx=step_idx,
                status=result.status,
            )

        return True

    # =================================================================
    # Step dispatch
    # =================================================================

    async def _execute_step(
        self,
        task_id: str,
        step: dict,
        prior_results: list[StepResult],
        *,
        workaround: str | None = None,
    ) -> StepResult:
        """Execute and record a single step via CC session."""
        from genesis.db.crud import task_steps

        step_idx = step["idx"]
        started_at = datetime.now(UTC).isoformat()

        await task_steps.update_step(
            self._db, task_id, step_idx,
            status="executing",
            started_at=started_at,
        )

        start = time.monotonic()
        try:
            result = await self._dispatch_step(
                task_id, step, prior_results, workaround=workaround,
            )
        except Exception as exc:
            duration = time.monotonic() - start
            logger.error(
                "Step %d of task %s failed: %s",
                step_idx, task_id, exc,
                exc_info=True,
            )
            result = StepResult(
                idx=step_idx,
                status="failed",
                result=str(exc),
                duration_s=duration,
            )

        completed_at = datetime.now(UTC).isoformat()
        await task_steps.update_step(
            self._db, task_id, step_idx,
            status=result.status,
            result_json=json.dumps({
                "result": result.result[:2000],
                "artifacts": result.artifacts,
                "blocker": result.blocker_description,
            }),
            cost_usd=result.cost_usd,
            model_used=result.model_used,
            session_id=result.session_id,
            completed_at=completed_at,
        )

        return result

    async def _dispatch_step(
        self,
        task_id: str,
        step: dict,
        prior_results: list[StepResult],
        *,
        workaround: str | None = None,
    ) -> StepResult:
        """Dispatch step to a CC session.

        All V3 steps use CC sessions. Multi-model router dispatch
        for research/analysis steps is deferred to V4.
        """
        from genesis.cc.types import CCInvocation, CCModel, EffortLevel

        step_idx = step["idx"]
        step_type_str = step.get("type", "code")
        try:
            step_type = StepType(step_type_str)
        except ValueError:
            step_type = StepType.CODE

        prompt = _dispatch.build_step_prompt(step, prior_results, workaround)

        # CODE steps use worktree working directory (Amendment #7)
        working_dir: str | None = None
        wt = self._worktree_paths.get(task_id)
        if wt and step_type == StepType.CODE:
            working_dir = str(wt)

        # Effort: HIGH for code/verification, MEDIUM otherwise
        effort = (
            EffortLevel.HIGH
            if step_type in (StepType.CODE, StepType.VERIFICATION)
            else EffortLevel.MEDIUM
        )

        invocation = CCInvocation(
            prompt=prompt,
            model=CCModel.SONNET,
            effort=effort,
            timeout_s=step_type.default_timeout_s,
            skip_permissions=True,
            working_dir=working_dir,
        )

        api_call_site_id = None
        if step_type in (StepType.RESEARCH, StepType.ANALYSIS, StepType.SYNTHESIS):
            api_call_site_id = "autonomous_executor_reasoning"

        if self._autonomous_dispatcher is not None:
            decision = await self._autonomous_dispatcher.route(
                AutonomousDispatchRequest(
                    subsystem="task_executor",
                    policy_id=f"executor_{step_type.value}",
                    action_label=f"task step {step_idx} ({step_type.value})",
                    messages=[{"role": "user", "content": prompt}],
                    cli_invocation=invocation,
                    api_call_site_id=api_call_site_id,
                    cli_fallback_allowed=True,
                    approval_required_for_cli=True,
                    context={
                        "task_id": task_id,
                        "step_idx": step_idx,
                        "step_type": step_type.value,
                    },
                ),
            )
            if decision.mode == "blocked":
                return StepResult(
                    idx=step_idx,
                    status="blocked",
                    result=decision.reason,
                    blocker_description=decision.reason,
                )
            if decision.mode == "api" and decision.output is not None:
                parsed = _dispatch.parse_step_output(decision.output.text)
                return StepResult(
                    idx=step_idx,
                    status=parsed.get("status", "completed"),
                    result=parsed.get("result", decision.output.text[:500]),
                    cost_usd=decision.output.cost_usd,
                    session_id=decision.output.session_id,
                    model_used=decision.output.model_used,
                    duration_s=0.0,
                    artifacts=parsed.get("artifacts", []),
                    blocker_description=parsed.get("blocker_description"),
                )

        start = time.monotonic()
        output = await self._invoker.run(invocation)
        duration = time.monotonic() - start

        if output.is_error:
            return StepResult(
                idx=step_idx,
                status="failed",
                result=output.error_message or output.text[:500],
                cost_usd=output.cost_usd,
                session_id=output.session_id,
                model_used=output.model_used,
                duration_s=duration,
            )

        # Parse structured output from CC response
        parsed = _dispatch.parse_step_output(output.text)

        return StepResult(
            idx=step_idx,
            status=parsed.get("status", "completed"),
            result=parsed.get("result", output.text[:500]),
            cost_usd=output.cost_usd,
            session_id=output.session_id,
            model_used=output.model_used,
            duration_s=duration,
            artifacts=parsed.get("artifacts", []),
            blocker_description=parsed.get("blocker_description"),
        )

    # =================================================================
    # Workaround recovery
    # =================================================================

    async def _try_workaround(
        self,
        task_id: str,
        step: dict,
        failed_result: StepResult,
        step_results: list[StepResult],
    ) -> StepResult | None:
        """Attempt workaround for a failed step. Returns new result or None."""
        if not self._workaround:
            return None

        try:
            wa_result = await self._workaround.search(
                step, failed_result.result, [],
            )
        except Exception:
            logger.error(
                "Workaround search failed for step %d",
                step["idx"], exc_info=True,
            )
            return None

        if wa_result is None or not wa_result.found or not wa_result.approach:
            return None

        # Retry with workaround context
        retry = await self._execute_step(
            task_id, step, step_results,
            workaround=wa_result.approach,
        )
        return retry if retry.status == "completed" else None

    # =================================================================
    # Worktree management (Amendment #7)
    # =================================================================

    async def _create_worktree(self, task_id: str) -> Path:
        """Create a git worktree for code task isolation."""
        wt_path = await _worktree.create_worktree(
            task_id, _REPO_ROOT, _WORKTREE_BASE,
        )
        self._worktree_paths[task_id] = wt_path
        return wt_path

    async def _cleanup_worktree(self, task_id: str) -> None:
        """Remove worktree if one was created for this task."""
        wt_path = self._worktree_paths.pop(task_id, None)
        if wt_path is None:
            return
        await _worktree.cleanup_worktree(wt_path, _REPO_ROOT)

    # =================================================================
    # Notification
    # =================================================================

    async def _notify(
        self,
        task_id: str,
        message: str,
        category: str,
    ) -> None:
        """Send notification via outreach pipeline."""
        if not self._outreach:
            logger.debug("No outreach pipeline, skipping: %s", message)
            return

        from genesis.outreach.types import OutreachCategory, OutreachRequest

        cat = {
            "blocker": OutreachCategory.BLOCKER,
            "alert": OutreachCategory.ALERT,
        }.get(category, OutreachCategory.ALERT)

        request = OutreachRequest(
            category=cat,
            topic=f"Task {task_id[:8]}",
            context=message,
            salience_score=0.9 if cat == OutreachCategory.BLOCKER else 0.5,
        )

        try:
            await self._outreach.submit(request)
        except Exception:
            logger.error(
                "Failed to send notification for task %s",
                task_id, exc_info=True,
            )

    # =================================================================
    # Delivery
    # =================================================================

    async def _deliver(
        self,
        task_id: str,
        deliverable: str,
        steps: list[dict],
    ) -> None:
        """Deliver task output. Pushes branch for code tasks."""
        has_code = any(s.get("type") == "code" for s in steps)
        wt_path = self._worktree_paths.get(task_id)

        if has_code and wt_path:
            branch = f"task/{task_id[:8]}"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "push", "-u", "origin", branch,
                    cwd=str(wt_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    logger.warning(
                        "Branch push failed for task %s: %s",
                        task_id, stderr.decode(errors="replace"),
                    )
                else:
                    logger.info("Pushed branch %s for task %s", branch, task_id)
                    await self._set_output(task_id, "branch", branch)
            except Exception:
                logger.error(
                    "Delivery failed for task %s", task_id, exc_info=True,
                )

    # =================================================================
    # Public API
    # =================================================================

    def cancel_task(self, task_id: str) -> bool:
        """Signal cancellation. Takes effect at next checkpoint."""
        event = self._cancel_events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a paused task."""
        # Clear per-task pause flag even if checkpoint hasn't created the event yet
        was_flagged = task_id in self._paused_tasks
        self._paused_tasks.discard(task_id)
        event = self._pause_events.get(task_id)
        if event is not None:
            event.set()
            return True
        return was_flagged

    def pause_task(self, task_id: str) -> bool:
        """Request pause at next checkpoint.

        Sets a per-task pause flag checked alongside the global
        ``runtime.paused`` in ``_checkpoint()``.
        """
        if task_id not in self._active_tasks:
            return False
        self._paused_tasks.add(task_id)
        return True

    def is_task_paused(self, task_id: str) -> bool:
        """Check if a per-task pause has been requested."""
        return task_id in self._paused_tasks

    def get_active_tasks(self) -> dict[str, str]:
        """Return ``{task_id: phase_str}`` for all active tasks."""
        terminal = {TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CANCELLED}
        return {
            tid: phase.value
            for tid, phase in self._active_tasks.items()
            if phase not in terminal
        }

    # =================================================================
    # Static helper delegates (backward compat for tests)
    # =================================================================

    _parse_step_output = staticmethod(_dispatch.parse_step_output)
    _synthesize_deliverable = staticmethod(_dispatch.synthesize_deliverable)
    _dominant_step_type = staticmethod(_dispatch.dominant_step_type)
    _create_fixup_step = staticmethod(_dispatch.create_fixup_step)

    async def _set_output(self, task_id: str, key: str, value: str) -> None:
        """JSON read-merge-write on the outputs column.

        If the existing value is a plain string (e.g. a plan path from
        the dispatcher), it is preserved under the ``plan_path`` key
        before being converted to a JSON envelope.
        """
        from genesis.db.crud import task_states

        task = await task_states.get_by_id(self._db, task_id)
        existing: dict = {}
        if task and task.get("outputs"):
            raw = task["outputs"]
            try:
                parsed = json.loads(raw)
                existing = parsed if isinstance(parsed, dict) else {"plan_path": str(parsed)}
            except (json.JSONDecodeError, ValueError):
                # Plain string — preserve it as plan_path
                existing = {"plan_path": raw}

        existing[key] = value
        await task_states.update(
            self._db, task_id, outputs=json.dumps(existing),
        )
