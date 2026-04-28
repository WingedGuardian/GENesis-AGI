"""Step dispatcher — routes individual task steps to CC sessions.

Extracted from ``engine.py`` to isolate step-level dispatch logic
(CC invocation, API/CLI routing, workaround recovery) from the
task-level state machine orchestration.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchRequest
from genesis.autonomy.executor import dispatch as _dispatch
from genesis.autonomy.executor.types import StepResult, StepType

logger = logging.getLogger(__name__)


class StepDispatcher:
    """Dispatch individual task steps to CC sessions or API providers.

    Handles:
    - CC invocation with model/effort selection
    - API-first routing via ``AutonomousDispatchRouter``
    - Call-site gating pre-check (skip if approval pending)
    - DB step recording (started_at, status, result_json, cost)
    - Workaround recovery for failed steps
    """

    def __init__(
        self,
        *,
        db: Any,
        invoker: Any,
        autonomous_dispatcher: Any | None = None,
        workaround_searcher: Any | None = None,
    ) -> None:
        self._db = db
        self._invoker = invoker
        self._autonomous_dispatcher = autonomous_dispatcher
        self._workaround = workaround_searcher

    async def execute_step(
        self,
        task_id: str,
        step: dict,
        prior_results: list[StepResult],
        *,
        workaround: str | None = None,
        worktree_path: Path | None = None,
    ) -> StepResult:
        """Execute and record a single step via CC session.

        Wraps ``dispatch_step`` with DB recording (started_at,
        status, result_json, cost, session_id, model_used).
        """
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
            result = await self.dispatch_step(
                task_id, step, prior_results,
                workaround=workaround,
                worktree_path=worktree_path,
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

    async def dispatch_step(
        self,
        task_id: str,
        step: dict,
        prior_results: list[StepResult],
        *,
        workaround: str | None = None,
        worktree_path: Path | None = None,
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

        # Load assigned resources (skills, procedures) for this step
        resources: str | None = None
        try:
            from genesis.autonomy.executor.resources import load_step_resources

            resources = await load_step_resources(self._db, step)
        except Exception:
            logger.debug("Step resource loading failed", exc_info=True)

        prompt = _dispatch.build_step_prompt(
            step, prior_results, workaround, resources=resources,
        )

        # CODE steps use worktree working directory (Amendment #7)
        working_dir: str | None = None
        if worktree_path and step_type == StepType.CODE:
            working_dir = str(worktree_path)

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
            # Call-site gating pre-check: if this executor step_type is
            # already pending approval, return a blocked StepResult
            # without creating a second approval request.
            executor_policy_id = f"executor_{step_type.value}"
            try:
                pending = await (
                    self._autonomous_dispatcher.approval_gate.find_site_pending(
                        subsystem="task_executor",
                        policy_id=executor_policy_id,
                    )
                )
            except Exception:
                logger.warning(
                    "find_site_pending failed for %s; proceeding without pre-check",
                    executor_policy_id, exc_info=True,
                )
                pending = None
            if pending is not None:
                # Check whether this pending request has already been
                # approved but not yet consumed.  Without this, the
                # pre-check blocks indefinitely even after the user
                # grants approval — route() never gets the chance to
                # consume the approved request.
                try:
                    approved = await (
                        self._autonomous_dispatcher
                        .approval_gate
                        .find_recently_approved(
                            subsystem="task_executor",
                            policy_id=executor_policy_id,
                        )
                    )
                except Exception:
                    logger.warning(
                        "find_recently_approved failed for %s; "
                        "treating as still pending",
                        executor_policy_id, exc_info=True,
                    )
                    approved = None

                if approved is None:
                    # Genuinely pending — no approval yet.
                    blocker = (
                        f"awaiting approval {pending.get('id')} for "
                        f"{step_type.value} step"
                    )
                    logger.info(
                        "Task %s step %d skipped — call site blocked "
                        "on approval %s",
                        task_id, step_idx, pending.get("id"),
                    )
                    return StepResult(
                        idx=step_idx,
                        status="blocked",
                        result=blocker,
                        blocker_description=blocker,
                    )
                # Approved but unconsumed — fall through to route()
                # which will consume it atomically.
                logger.info(
                    "Task %s step %d has approved request %s — "
                    "proceeding to route()",
                    task_id, step_idx, approved.get("id"),
                )

            decision = await self._autonomous_dispatcher.route(
                AutonomousDispatchRequest(
                    subsystem="task_executor",
                    policy_id=executor_policy_id,
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

    async def try_workaround(
        self,
        task_id: str,
        step: dict,
        failed_result: StepResult,
        step_results: list[StepResult],
        *,
        worktree_path: Path | None = None,
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
        retry = await self.execute_step(
            task_id, step, step_results,
            workaround=wa_result.approach,
            worktree_path=worktree_path,
        )
        return retry if retry.status == "completed" else None
