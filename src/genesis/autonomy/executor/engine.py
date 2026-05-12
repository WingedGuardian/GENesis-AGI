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
- Recovery:      Phase resume — skip REVIEWING/PLANNING on recovery
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from genesis.autonomy.executor import dispatch as _dispatch
from genesis.autonomy.executor import worktree_mgr as _worktree
from genesis.autonomy.executor.step_dispatcher import StepDispatcher
from genesis.autonomy.executor.types import (
    ExecutionTrace,
    InvalidTransitionError,
    StepResult,
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
        research_searcher: Any | None = None,
        router: Any | None = None,
        tracer: Any | None = None,
        outreach_pipeline: Any | None = None,
        event_bus: Any | None = None,
        runtime: Any | None = None,
        autonomous_dispatcher: Any | None = None,
        exec_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._db = db
        self._invoker = invoker
        self._decomposer = decomposer
        self._reviewer = reviewer
        self._router = router
        self._tracer = tracer
        self._outreach = outreach_pipeline
        self._event_bus = event_bus
        self._runtime = runtime
        self._autonomous_dispatcher = autonomous_dispatcher
        self._exec_semaphore = exec_semaphore

        # Step dispatch (extracted to StepDispatcher)
        self._step_dispatcher = StepDispatcher(
            db=db,
            invoker=invoker,
            autonomous_dispatcher=autonomous_dispatcher,
            workaround_searcher=workaround_searcher,
            research_searcher=research_searcher,
        )

        # In-memory state
        self._active_tasks: dict[str, TaskPhase] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._paused_tasks: set[str] = set()
        self._worktree_paths: dict[str, Path] = {}
        self._semaphore_released: set[str] = set()  # tracks tasks whose semaphore was released during pause

    # =================================================================
    # Main lifecycle
    # =================================================================

    async def execute(self, task_id: str) -> bool:
        """Run a task through its full lifecycle.

        Returns ``True`` on successful completion, ``False`` on
        blocker/failure/cancellation.

        Supports recovery resume: if the task was previously blocked in
        EXECUTING, VERIFYING, or BLOCKED phase, existing step results
        are loaded from the DB and REVIEWING/PLANNING are skipped.
        """
        from genesis.db.crud import task_states, task_steps

        task = await task_states.get_by_id(self._db, task_id)
        if task is None:
            logger.error("Task %s not found in DB", task_id)
            return False

        # Resolve plan path from outputs (may be plain string or JSON)
        raw_outputs = task.get("outputs") or ""
        plan_path = raw_outputs
        if raw_outputs:
            try:
                parsed = json.loads(raw_outputs)
                if isinstance(parsed, dict):
                    plan_path = parsed.get("plan_path", raw_outputs)
            except (json.JSONDecodeError, ValueError):
                pass  # plain string, use as-is

        if not plan_path:
            logger.error("Task %s has no plan path in outputs column", task_id)
            await self._fail_task(task_id, "No plan path specified")
            return False

        try:
            plan_content = Path(plan_path).expanduser().read_text(encoding="utf-8")
        except OSError:
            logger.error("Cannot read plan at %s", plan_path, exc_info=True)
            await self._fail_task(task_id, f"Cannot read plan: {plan_path}")
            return False

        description = task.get("description", "")

        # Determine recovery phase from DB
        db_phase_str = task.get("current_phase", "pending")
        _RESUMABLE_PHASES = {"executing", "verifying", "blocked"}
        resuming = db_phase_str in _RESUMABLE_PHASES

        # Register task at current phase (recovery) or PENDING (fresh)
        if resuming:
            self._active_tasks[task_id] = TaskPhase(db_phase_str)
            logger.info(
                "Task %s: resuming from %s phase (skipping review/plan)",
                task_id, db_phase_str,
            )
        else:
            self._active_tasks[task_id] = TaskPhase.PENDING

        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event

        # Start trace
        trace: ExecutionTrace | None = None
        if self._tracer:
            trace = self._tracer.start_trace(task_id, "genesis", description)

        try:
            if resuming:
                # Recovery path: load existing steps from DB
                existing_rows = await task_steps.get_steps_for_task(
                    self._db, task_id,
                )
                steps = [
                    {
                        "idx": row["step_idx"],
                        "type": row.get("step_type", "code"),
                        "description": row.get("description", ""),
                        "complexity": "medium",
                    }
                    for row in existing_rows
                ]

                # Reconstruct completed StepResults from persisted data
                step_results: list[StepResult] = []
                completed_indices: set[int] = set()
                for row in existing_rows:
                    if row.get("status") == "completed" and row.get("result_json"):
                        try:
                            rj = json.loads(row["result_json"])
                        except (json.JSONDecodeError, ValueError):
                            rj = {}
                        sr = StepResult(
                            idx=row["step_idx"],
                            status="completed",
                            result=rj.get("result", ""),
                            cost_usd=row.get("cost_usd", 0.0) or 0.0,
                            session_id=row.get("session_id"),
                            model_used=row.get("model_used", ""),
                            artifacts=rj.get("artifacts", []),
                        )
                        step_results.append(sr)
                        completed_indices.add(row["step_idx"])

                # Recover or create worktree for code tasks
                has_code = any(s.get("type") == "code" for s in steps)
                if has_code:
                    recovered = await self._recover_worktree(task_id, task)
                    if not recovered:
                        await self._create_worktree(task_id)

                # Filter to only pending/failed steps
                remaining_steps = [
                    s for s in steps if s["idx"] not in completed_indices
                ]

                logger.info(
                    "Task %s: recovered %d completed steps, %d remaining",
                    task_id, len(completed_indices), len(remaining_steps),
                )
            else:
                # Fresh path: REVIEWING -> PLANNING
                # --- REVIEWING ---
                await self._transition(task_id, TaskPhase.REVIEWING)
                review = await self._reviewer.review_plan(
                    plan_content, description,
                )

                if not review.passed:
                    gap_text = (
                        "; ".join(review.gaps) if review.gaps else "unspecified"
                    )
                    await self._persist_blocker(
                        task_id,
                        f"Plan review found gaps: {gap_text}",
                        TaskPhase.REVIEWING,
                    )
                    return False

                # --- PRE-MORTEM (fail-open) ---
                pm = await self._reviewer.pre_mortem(plan_content, description)
                if pm is not None:
                    from genesis.autonomy.executor.review import (
                        _PM_BLOCK_THRESHOLD,
                        _PM_MITIGATE_THRESHOLD,
                    )

                    if pm.confidence < _PM_BLOCK_THRESHOLD:
                        modes = "; ".join(pm.failure_modes[:3])
                        await self._persist_blocker(
                            task_id,
                            f"Pre-mortem confidence {pm.confidence}% "
                            f"(threshold {_PM_BLOCK_THRESHOLD}%): {modes}",
                            TaskPhase.REVIEWING,
                        )
                        return False

                    if pm.confidence < _PM_MITIGATE_THRESHOLD:
                        if pm.mitigations:
                            mitigation_text = "\n".join(
                                f"- {m}" for m in pm.mitigations
                            )
                            plan_content = (
                                f"{plan_content}\n\n"
                                f"## Pre-Mortem Mitigations "
                                f"(confidence: {pm.confidence}%)\n\n"
                                f"{mitigation_text}\n"
                            )
                            logger.info(
                                "Pre-mortem injected %d mitigations (conf=%d%%)",
                                len(pm.mitigations), pm.confidence,
                            )
                        else:
                            logger.info(
                                "Pre-mortem confidence %d%% (medium) but no mitigations provided",
                                pm.confidence,
                            )

                    await self._set_output(
                        task_id, "pre_mortem",
                        json.dumps({
                            "confidence": pm.confidence,
                            "failure_modes": pm.failure_modes,
                            "mitigations": pm.mitigations,
                        }),
                    )

                # --- PLANNING ---
                await self._transition(task_id, TaskPhase.PLANNING)
                steps = await self._decomposer.decompose(
                    plan_content, description,
                )

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

                step_results = []
                remaining_steps = steps

            # --- EXECUTING ---
            # When recovering from VERIFYING/BLOCKED, remaining_steps
            # is typically empty (all steps completed in prior run).
            # The loop below is a no-op, and we fall through directly
            # to the VERIFYING phase with the recovered step_results.
            await self._transition(task_id, TaskPhase.EXECUTING)

            for step in remaining_steps:
                # Check cancel before each step
                if cancel_event.is_set():
                    await self._transition(task_id, TaskPhase.CANCELLED)
                    return False

                result = await self._step_dispatcher.execute_step(
                    task_id, step, step_results,
                    worktree_path=self._worktree_paths.get(task_id),
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

                # Handle failed step -- layered recovery
                if result.status == "failed":
                    wt_path = self._worktree_paths.get(task_id)

                    # Layer 1: procedural memory workaround
                    recovered = await self._step_dispatcher.try_workaround(
                        task_id, step, result, step_results,
                        worktree_path=wt_path,
                    )
                    if recovered is not None:
                        step_results[-1] = recovered
                        if trace:
                            self._tracer.record_step(trace, recovered)
                        continue

                    # Layer 2: inline due diligence (quick web+memory)
                    dd_context = await self._step_dispatcher.try_due_diligence(
                        step, result,
                    )
                    if dd_context:
                        dd_retry = await self._step_dispatcher.execute_step(
                            task_id, step, step_results,
                            workaround=dd_context,
                            worktree_path=wt_path,
                        )
                        if dd_retry.status == "completed":
                            step_results[-1] = dd_retry
                            if trace:
                                self._tracer.record_step(trace, dd_retry)
                            continue
                        if dd_retry.status == "blocked":
                            await self._persist_blocker(
                                task_id,
                                dd_retry.blocker_description or "Blocked during due diligence retry",
                                TaskPhase.EXECUTING,
                                step_idx=step["idx"],
                            )
                            return False

                    # Layer 3: full research session
                    researched, research_result = (
                        await self._step_dispatcher.try_research(
                            task_id, step, result, step_results,
                            due_diligence_results=dd_context,
                            worktree_path=wt_path,
                        )
                    )
                    if researched is not None:
                        step_results[-1] = researched
                        if trace:
                            self._tracer.record_step(trace, researched)
                        continue

                    # Layer 4: exit gate loop (cap 10 — safety net only)
                    exit_decision = None
                    prior_rejections: list[dict] = []
                    for _gate_cycle in range(10):
                        exit_decision = await self._challenge_failure(
                            task_id, step, result, research_result,
                            prior_rejections,
                        )
                        if exit_decision.get("verdict") == "accept":
                            break
                        # Exit gate rejected — try its suggestion
                        prior_rejections.append(exit_decision)
                        suggested = exit_decision.get("suggested_approach", "")
                        if suggested:
                            retry = await self._step_dispatcher.execute_step(
                                task_id, step, step_results,
                                workaround=suggested,
                                worktree_path=wt_path,
                            )
                            if retry.status == "completed":
                                step_results[-1] = retry
                                if trace:
                                    self._tracer.record_step(trace, retry)
                                break
                            if retry.status == "blocked":
                                await self._persist_blocker(
                                    task_id,
                                    retry.blocker_description or "Blocked during exit gate retry",
                                    TaskPhase.EXECUTING,
                                    step_idx=step["idx"],
                                )
                                return False
                            # Update result for next gate cycle so it judges fresh evidence
                            result = retry
                    else:
                        # Hit cap 10 — force accept (safety net)
                        logger.warning(
                            "Exit gate cap reached for task %s step %d",
                            task_id, step["idx"],
                        )

                    if step_results[-1].status == "completed":
                        continue  # one of the gate retries worked

                    # All recovery exhausted. Record challenge + fail.
                    await self._record_challenge(
                        task_id, step, result, research_result, exit_decision,
                    )
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
                    worktree_path=self._worktree_paths.get(task_id),
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
                        plan_content=plan_content,
                    )
                    fixup_result = await self._step_dispatcher.execute_step(
                        task_id, fixup, step_results,
                        worktree_path=self._worktree_paths.get(task_id),
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
    # Exit gate + challenge recording
    # =================================================================

    async def _challenge_failure(
        self,
        task_id: str,
        step: dict,
        result: StepResult,
        research_result: Any,
        prior_rejections: list[dict],
    ) -> dict:
        """Adversarial exit gate — challenges the failure before accepting it."""
        if not self._router:
            # No router → can't run exit gate → accept by default
            return {"verdict": "accept", "confirmed_blockers": ["No exit gate available"]}

        from pathlib import Path as _Path

        prompt_path = _Path(__file__).resolve().parent / "prompts" / "exit_gate.md"
        try:
            template = prompt_path.read_text()
        except FileNotFoundError:
            return {"verdict": "accept", "confirmed_blockers": ["Exit gate prompt missing"]}

        desc = step.get("description", step.get("title", "unknown step"))
        research_conclusion = ""
        concrete_blockers_text = "None identified"
        if research_result:
            research_conclusion = research_result.clues or "No clues found"
            if research_result.concrete_blockers:
                concrete_blockers_text = "\n".join(
                    f"- {b}" for b in research_result.concrete_blockers
                )

        rejections_text = "None (first attempt)"
        if prior_rejections:
            rejections_text = "\n".join(
                f"- Attempt {i+1}: {r.get('reason', '?')}"
                for i, r in enumerate(prior_rejections)
            )

        prompt = (
            template
            .replace("{{step_description}}", desc)
            .replace("{{error_text}}", result.result[:2000])
            .replace("{{research_conclusion}}", research_conclusion)
            .replace("{{concrete_blockers}}", concrete_blockers_text)
            .replace("{{prior_rejections}}", rejections_text)
        )

        try:
            llm_result = await self._router.route_call(
                "failure_exit_gate",
                [{"role": "user", "content": prompt}],
            )
            text = (llm_result.content if hasattr(llm_result, "content") else str(llm_result)) or ""
            # Parse JSON from response
            parsed = self._parse_gate_response(text)
            if parsed:
                return parsed
        except Exception:
            logger.exception("Exit gate LLM call failed for task %s", task_id)

        # On failure, default to accept (don't infinite loop)
        return {"verdict": "accept", "confirmed_blockers": ["Exit gate call failed"]}

    @staticmethod
    def _parse_gate_response(text: str) -> dict | None:
        """Extract JSON verdict from exit gate response."""
        import re

        json_block = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL)
        if json_block:
            try:
                parsed = json.loads(json_block.group(1))
                if "verdict" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass

        # Try bare JSON object
        for i in range(len(text) - 1, -1, -1):
            if text[i] == "}":
                depth = 0
                for j in range(i, -1, -1):
                    if text[j] == "}":
                        depth += 1
                    elif text[j] == "{":
                        depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[j : i + 1])
                            if "verdict" in parsed:
                                return parsed
                        except json.JSONDecodeError:
                            break
                break
        return None

    async def _record_challenge(
        self,
        task_id: str,
        step: dict,
        result: StepResult,
        research_result: Any,
        exit_decision: dict | None,
    ) -> None:
        """Record a permanent execution_challenge observation + follow-up."""
        desc = step.get("description", step.get("title", "unknown step"))

        # Build challenge content
        blockers = []
        clues = None
        what_needs_to_change = None
        if research_result:
            blockers = research_result.concrete_blockers or []
            clues = research_result.clues
        if exit_decision and exit_decision.get("verdict") == "accept":
            what_needs_to_change = exit_decision.get("what_needs_to_change")
            confirmed = exit_decision.get("confirmed_blockers", [])
            if confirmed:
                blockers = confirmed

        content = json.dumps({
            "task_id": task_id,
            "step_description": desc,
            "error": result.result[:1000],
            "concrete_blockers": blockers,
            "clues": clues,
            "what_needs_to_change": what_needs_to_change,
            "research_session_id": getattr(research_result, "session_id", None),
        })

        # Create permanent observation
        try:
            import uuid

            from genesis.db.crud import observations

            await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="task_executor",
                type="execution_challenge",
                content=content,
                priority="high",
                created_at=datetime.now(UTC).isoformat(),
            )
            logger.info(
                "Recorded execution_challenge observation for task %s step %s",
                task_id, step.get("idx", "?"),
            )
        except Exception:
            logger.exception("Failed to record execution challenge observation")

        # Create follow-up for ego evaluation
        try:
            from genesis.db.crud import follow_ups

            await follow_ups.create(
                self._db,
                content=f"Execution challenge: step '{desc}' failed after deep research. "
                f"Blockers: {', '.join(blockers) if blockers else 'unknown'}",
                source="task_executor",
                strategy="ego_judgment",
                reason=content,
            )
        except Exception:
            logger.exception("Failed to create challenge follow-up")

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

            # Release execution semaphore so another task can run
            if self._exec_semaphore:
                self._exec_semaphore.release()
                self._semaphore_released.add(task_id)

            pause_event = asyncio.Event()
            self._pause_events[task_id] = pause_event

            # Wait for resume OR cancel
            while not pause_event.is_set():
                if cancel and cancel.is_set():
                    with contextlib.suppress(InvalidTransitionError):
                        await self._transition(task_id, TaskPhase.CANCELLED)
                    # semaphore_released stays set — dispatcher will
                    # skip release in _guarded_execute finally block
                    return False
                await asyncio.sleep(0.1)

            # Reacquire semaphore before resuming
            if self._exec_semaphore:
                await self._exec_semaphore.acquire()
                self._semaphore_released.discard(task_id)

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
    # Worktree management (Amendment #7)
    # =================================================================

    async def _create_worktree(self, task_id: str) -> Path:
        """Create a git worktree for code task isolation."""
        wt_path = await _worktree.create_worktree(
            task_id, _REPO_ROOT, _WORKTREE_BASE,
        )
        self._worktree_paths[task_id] = wt_path
        await self._set_output(task_id, "worktree_path", str(wt_path))
        return wt_path

    async def _recover_worktree(
        self, task_id: str, task: dict,
    ) -> bool:
        """Recover a worktree from persisted state.

        Returns True if a valid worktree was found or recreated.
        Falls back to False so the caller can create a fresh one.
        """
        raw_outputs = task.get("outputs") or ""
        try:
            parsed = json.loads(raw_outputs)
            wt_path_str = (
                parsed.get("worktree_path")
                if isinstance(parsed, dict)
                else None
            )
        except (json.JSONDecodeError, ValueError):
            wt_path_str = None

        if not wt_path_str:
            return False

        wt_path = Path(wt_path_str)
        if await _worktree.verify_worktree(wt_path):
            self._worktree_paths[task_id] = wt_path
            logger.info(
                "Recovered existing worktree at %s for task %s",
                wt_path, task_id,
            )
            return True

        # Worktree gone but branch might still exist — recreate
        try:
            wt_path = await _worktree.create_worktree(
                task_id, _REPO_ROOT, _WORKTREE_BASE,
            )
            self._worktree_paths[task_id] = wt_path
            await self._set_output(task_id, "worktree_path", str(wt_path))
            logger.info(
                "Recreated worktree at %s for task %s (branch recovered)",
                wt_path, task_id,
            )
            return True
        except RuntimeError:
            logger.warning(
                "Worktree recovery failed for task %s", task_id,
            )
            return False

    async def _cleanup_worktree(self, task_id: str) -> None:
        """Remove worktree if one was created for this task."""
        wt_path = self._worktree_paths.pop(task_id, None)
        if wt_path is None:
            return
        await _worktree.cleanup_worktree(wt_path, _REPO_ROOT)
        with contextlib.suppress(Exception):
            await self._set_output(task_id, "worktree_path", "")

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
