"""BrainstormRunner — schedules and executes daily brainstorm sessions."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import brainstorm as brainstorm_crud
from genesis.db.crud import surplus as surplus_crud
from genesis.surplus.executor import StubExecutor
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.types import (
    ComputeTier,
    SurplusExecutor,
    SurplusTask,
    TaskStatus,
    TaskType,
)

logger = logging.getLogger(__name__)

# Map brainstorm task types to brainstorm_log session_type values
_SESSION_TYPE_MAP = {
    TaskType.BRAINSTORM_USER: "upgrade_user",
    TaskType.BRAINSTORM_SELF: "upgrade_self",
    TaskType.CODE_AUDIT: "code_audit",
    TaskType.INFRASTRUCTURE_MONITOR: "infra_monitor",
    TaskType.SELF_UNBLOCK: "self_unblock",
}


class BrainstormRunner:
    """Schedules mandatory daily brainstorm sessions and writes results to staging."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        queue: SurplusQueue,
        *,
        executor: SurplusExecutor | None = None,
        clock=None,
    ):
        self._db = db
        self._queue = queue
        self._executor = executor or StubExecutor()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def schedule_daily_brainstorms(self) -> None:
        """Enqueue today's brainstorm sessions if not already scheduled."""
        today = self._clock().date().isoformat()

        for task_type, drive in [
            (TaskType.BRAINSTORM_USER, "cooperation"),
            (TaskType.BRAINSTORM_SELF, "competence"),
            (TaskType.SELF_UNBLOCK, "competence"),
        ]:
            session_type = _SESSION_TYPE_MAP[task_type]

            # Check brainstorm_log for today's completed sessions
            existing = await brainstorm_crud.list_by_type(self._db, session_type, limit=1)
            if existing and existing[0]["created_at"].startswith(today):
                logger.debug("Brainstorm %s already logged for %s", session_type, today)
                continue

            # Check surplus_tasks for already-queued tasks today
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM surplus_tasks WHERE task_type = ? AND created_at LIKE ?",
                (str(task_type), today + "%"),
            )
            row = await cursor.fetchone()
            if row[0] > 0:
                logger.debug("Brainstorm %s already queued for %s", session_type, today)
                continue

            await self._queue.enqueue(
                task_type=task_type,
                compute_tier=ComputeTier.FREE_API,
                priority=0.8,
                drive_alignment=drive,
                payload=json.dumps({"scheduled_date": today}),
            )
            logger.info("Enqueued brainstorm %s for %s", session_type, today)

    async def execute_brainstorm(
        self,
        task_type: TaskType,
        drive_alignment: str,
    ) -> str | None:
        """Execute a brainstorm session: run executor, write to staging + log."""
        task = SurplusTask(
            id=str(uuid.uuid4()),
            task_type=task_type,
            compute_tier=ComputeTier.FREE_API,
            priority=0.8,
            drive_alignment=drive_alignment,
            status=TaskStatus.RUNNING,
            created_at=self._clock().isoformat(),
        )

        result = await self._executor.execute(task)
        if not result.success:
            logger.warning("Brainstorm %s failed: %s", task_type, result.error)
            return None

        # Skip persisting stub results — they're noise (confidence=0.0 placeholders)
        first_insight = result.insights[0] if result.insights else {}
        if first_insight.get("generating_model") == "stub":
            logger.debug("Skipping stub brainstorm result (no real executor wired)")
            return None

        from datetime import timedelta
        now = self._clock()
        now_iso = now.isoformat()
        staging_id = str(uuid.uuid4())

        insight = result.insights[0] if result.insights else {}
        content = result.content or ""

        # Route through intake pipeline (atomize → score → knowledge base)
        try:
            from genesis.surplus.intake import run_intake, source_for_task_type
            source = source_for_task_type(str(task_type))
            await run_intake(
                content=content,
                source=source,
                source_task_type=str(task_type),
                generating_model=insight.get("generating_model", "stub"),
                db=self._db,
            )
        except Exception:
            # Fallback: write to surplus_insights staging (old behavior)
            logger.warning("Intake pipeline failed in brainstorm — falling back to staging", exc_info=True)
            ttl = (now + timedelta(days=7)).isoformat()
            await surplus_crud.create(
                self._db,
                id=staging_id,
                content=content,
                source_task_type=str(task_type),
                generating_model=insight.get("generating_model", "stub"),
                drive_alignment=drive_alignment,
                confidence=insight.get("confidence", 0.0),
                created_at=now_iso,
                ttl=ttl,
            )

        # Write to brainstorm_log
        session_type = _SESSION_TYPE_MAP.get(task_type, str(task_type))
        await brainstorm_crud.create(
            self._db,
            id=str(uuid.uuid4()),
            session_type=session_type,
            model_used=insight.get("generating_model", "stub"),
            outputs=result.insights,
            staging_ids=[staging_id],
            created_at=now_iso,
        )

        return staging_id
