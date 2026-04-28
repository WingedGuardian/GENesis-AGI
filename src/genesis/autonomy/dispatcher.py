"""Task dispatcher --- submit, poll, recover, and dedup task execution.

Primary entry point for task execution. Called by the task_submit MCP tool
(immediate dispatch) and by a background polling loop (observation-based
secondary path per Amendment #2).

Implements:
- Amendment #2:  Immediate dispatch via submit(), surplus polls observations
- Amendment #9:  Path validation for plan files
- Amendment #11: Dispatch dedup via observation resolution
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from genesis.autonomy.executor.types import TaskPhase

logger = logging.getLogger(__name__)

# Allowed directories for plan files (Amendment #9)
_ALLOWED_PLAN_DIRS = [
    Path.home() / ".genesis" / "plans",
    Path.home() / ".claude" / "plans",
]

# Terminal phases that should not be re-dispatched
_TERMINAL_PHASES = frozenset({
    TaskPhase.COMPLETED.value,
    TaskPhase.FAILED.value,
    TaskPhase.CANCELLED.value,
})


def _validate_plan_path(path_str: str) -> Path:
    """Validate that a plan path is under allowed directories.

    Raises ValueError for paths outside allowed directories.
    Raises FileNotFoundError if the plan file does not exist.
    """
    resolved = Path(path_str).resolve()
    if not any(resolved.is_relative_to(d) for d in _ALLOWED_PLAN_DIRS):
        raise ValueError(
            f"Plan path outside allowed directories: {path_str}. "
            f"Allowed: {[str(d) for d in _ALLOWED_PLAN_DIRS]}"
        )
    if not resolved.exists():
        raise FileNotFoundError(f"Plan file not found: {path_str}")
    return resolved


class TaskDispatcher:
    """Submits tasks for execution with dedup and crash recovery."""

    def __init__(
        self,
        *,
        db: Any,
        executor: Any,
        event_bus: Any | None = None,
    ) -> None:
        self._db = db
        self._executor = executor
        self._event_bus = event_bus
        self._dispatched: set[str] = set()  # in-memory dedup guard

    async def submit(
        self,
        plan_path: str,
        description: str,
        *,
        source: str = "user",
    ) -> str:
        """Submit a task for immediate execution.

        Validates the plan path, creates a task_states row, and dispatches
        the executor via tracked_task. Returns the task_id.

        Raises ValueError for invalid plan paths.
        Raises FileNotFoundError for missing plan files.
        """
        from genesis.db.crud import task_states
        from genesis.util.tasks import tracked_task

        # Amendment #9: path validation
        resolved_path = _validate_plan_path(plan_path)

        task_id = f"t-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC).isoformat()

        await task_states.create(
            self._db,
            task_id=task_id,
            description=description,
            current_phase=TaskPhase.PENDING.value,
            decisions=None,
            blockers=None,
            outputs=str(resolved_path),
            session_id=None,
            created_at=now,
        )

        self._dispatched.add(task_id)

        # Dispatch immediately via tracked_task (Amendment #2)
        tracked_task(
            self._executor.execute(task_id),
            name=f"task-{task_id}",
        )

        logger.info(
            "Task %s dispatched (source=%s, plan=%s)",
            task_id, source, resolved_path,
        )

        if self._event_bus:
            try:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.AUTONOMY,
                    Severity.INFO,
                    "task.dispatched",
                    f"Task {task_id} dispatched: {description[:100]}",
                    task_id=task_id,
                    source=source,
                )
            except Exception:
                logger.error(
                    "Failed to emit task.dispatched event",
                    exc_info=True,
                )

        return task_id

    async def dispatch_cycle(self) -> int:
        """Poll for pending tasks and task_detected observations.

        Two pickup paths:
        1. DB scan for PENDING tasks not yet dispatched (picks up tasks
           created by MCP tool's DB-direct fallback path).
        2. Observation scan for task_detected observations with plan_path
           metadata (Amendment #11).

        Returns the count of newly dispatched tasks.
        """
        from genesis.db.crud import observations, task_states
        from genesis.util.tasks import tracked_task

        dispatched = 0

        # Fetch active tasks once — reused by both paths
        try:
            active = await task_states.list_active(self._db)
        except Exception:
            logger.error("Failed to list active tasks", exc_info=True)
            active = []

        # --- Path 1: Pick up PENDING tasks from DB (MCP-submitted) ---
        for task in active:
            task_id = task["task_id"]
            phase = task.get("current_phase", "")
            if phase != TaskPhase.PENDING.value:
                continue

            plan_path = task.get("outputs") or ""
            if not plan_path:
                if task_id not in self._dispatched:
                    logger.warning(
                        "Pending task %s has no plan path, skipping", task_id,
                    )
                continue

            # Allow re-dispatch of PENDING tasks that were previously
            # dispatched and then reset. A PENDING task in the DB cannot
            # be concurrently executing — the executor transitions out
            # of PENDING within the first few lines of execute().
            if task_id in self._dispatched:
                logger.info(
                    "Re-dispatching reset task %s (was in _dispatched)",
                    task_id,
                )

            self._dispatched.add(task_id)
            tracked_task(
                self._executor.execute(task_id),
                name=f"task-{task_id}",
            )
            dispatched += 1

        # --- Path 1b: Resume BLOCKED tasks with approved approvals ---
        for task in active:
            task_id = task["task_id"]
            phase = task.get("current_phase", "")
            if phase != TaskPhase.BLOCKED.value:
                continue

            # Check if there's an approved-but-unconsumed approval for this task
            try:
                cursor = await self._db.execute(
                    """SELECT id FROM approval_requests
                       WHERE status = 'approved' AND consumed_at IS NULL
                         AND context LIKE ?
                       LIMIT 1""",
                    (f'%"task_id": "{task_id}"%',),
                )
                row = await cursor.fetchone()
            except Exception:
                logger.error(
                    "Failed to check approvals for blocked task %s",
                    task_id, exc_info=True,
                )
                continue

            if row is None:
                continue

            logger.info(
                "Resuming blocked task %s (approved approval found)",
                task_id,
            )
            self._dispatched.add(task_id)
            tracked_task(
                self._executor.execute(task_id),
                name=f"task-{task_id}-resume",
            )
            dispatched += 1

        # --- Path 2: Observation-based task pickup ---
        try:
            pending_obs = await observations.query(
                self._db,
                type="task_detected",
                resolved=False,
                limit=10,
            )
        except Exception:
            logger.error("Failed to query task observations", exc_info=True)
            return dispatched

        active_descriptions = {t.get("description", "") for t in active}

        for obs in pending_obs:
            obs_id = obs["id"]
            content = obs.get("content", "")

            # Dedup: check if already dispatched or task exists
            if obs_id in self._dispatched:
                continue

            # Simple dedup: skip if description matches any active task
            if content in active_descriptions:
                await self._resolve_observation(obs_id, "already_active")
                continue

            # Observation-sourced tasks need a plan path — for now, skip
            # tasks that don't have one. Full auto-planning is V4.
            plan_path = obs.get("metadata", {}).get("plan_path") if isinstance(obs.get("metadata"), dict) else None
            if not plan_path:
                logger.debug(
                    "Skipping observation %s: no plan_path in metadata",
                    obs_id,
                )
                continue

            try:
                task_id = await self.submit(
                    plan_path, content, source="observation",
                )
                await self._resolve_observation(obs_id, f"dispatched:{task_id}")
                dispatched += 1
            except (ValueError, FileNotFoundError) as exc:
                logger.warning(
                    "Cannot dispatch observation %s: %s", obs_id, exc,
                )
                await self._resolve_observation(obs_id, f"invalid:{exc}")
            except Exception:
                logger.error(
                    "Failed to dispatch observation %s",
                    obs_id, exc_info=True,
                )

        return dispatched

    async def recover_incomplete(self) -> int:
        """Recover tasks that were interrupted (crash recovery).

        Finds non-terminal tasks and re-dispatches them. Called during
        bootstrap to recover from crashes.

        Returns the count of recovered tasks.
        """
        from genesis.db.crud import task_states
        from genesis.util.tasks import tracked_task

        try:
            active = await task_states.list_active(self._db)
        except Exception:
            logger.error("Failed to list active tasks for recovery", exc_info=True)
            return 0

        recovered = 0
        for task in active:
            task_id = task["task_id"]
            phase = task.get("current_phase", "")

            if phase in _TERMINAL_PHASES:
                continue

            if task_id in self._dispatched:
                continue

            if phase == TaskPhase.BLOCKED.value:
                # Check if there's an approved-but-unconsumed approval
                try:
                    cursor = await self._db.execute(
                        """SELECT id FROM approval_requests
                           WHERE status = 'approved' AND consumed_at IS NULL
                             AND context LIKE ?
                           LIMIT 1""",
                        (f'%"task_id": "{task_id}"%',),
                    )
                    approved_row = await cursor.fetchone()
                except Exception:
                    logger.error(
                        "Failed to check approvals for blocked task %s",
                        task_id, exc_info=True,
                    )
                    approved_row = None

                if approved_row:
                    # Approval granted — re-dispatch for execution
                    logger.info(
                        "Resuming blocked task %s (approved approval found)",
                        task_id,
                    )
                    self._dispatched.add(task_id)
                    tracked_task(
                        self._executor.execute(task_id),
                        name=f"task-resume-{task_id}",
                    )
                    recovered += 1
                else:
                    # Still blocked, just mark as known
                    logger.info("Found blocked task %s, re-notifying", task_id)
                    self._dispatched.add(task_id)
                    recovered += 1
                continue

            # Re-dispatch executing/reviewing/planning/etc tasks
            logger.info(
                "Recovering task %s from phase %s", task_id, phase,
            )
            self._dispatched.add(task_id)
            tracked_task(
                self._executor.execute(task_id),
                name=f"task-recover-{task_id}",
            )
            recovered += 1

        if recovered:
            logger.info("Recovered %d incomplete tasks", recovered)

        return recovered

    async def _resolve_observation(
        self, obs_id: str, notes: str,
    ) -> None:
        """Resolve an observation after dispatch (Amendment #11)."""
        from genesis.db.crud import observations

        now = datetime.now(UTC).isoformat()
        try:
            await observations.resolve(
                self._db, obs_id,
                resolved_at=now,
                resolution_notes=notes,
            )
        except Exception:
            logger.error(
                "Failed to resolve observation %s", obs_id,
                exc_info=True,
            )
