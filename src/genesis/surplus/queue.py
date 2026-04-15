"""SurplusQueue — high-level queue wrapping surplus_tasks CRUD with drive-weight priority."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import surplus_tasks
from genesis.surplus.types import ComputeTier, SurplusTask, TaskStatus, TaskType


class SurplusQueue:
    """Priority queue for surplus tasks, backed by the surplus_tasks table.

    Priority is base_priority × drive_weight (from drive_weights table).
    """

    def __init__(self, db: aiosqlite.Connection, *, clock=None):
        self._db = db
        self._clock = clock or (lambda: datetime.now(UTC))

    async def enqueue(
        self,
        task_type: TaskType | str,
        compute_tier: ComputeTier | str,
        priority: float,
        drive_alignment: str,
        payload: str | None = None,
        not_before: str | None = None,
    ) -> str:
        """Add a task to the queue. Rejects NEVER tier."""
        tier = ComputeTier(compute_tier) if isinstance(compute_tier, str) else compute_tier
        if tier == ComputeTier.NEVER:
            msg = "Cannot enqueue surplus tasks with NEVER compute tier"
            raise ValueError(msg)

        effective_priority = await self._apply_drive_weight(priority, drive_alignment)
        task_id = str(uuid.uuid4())
        await surplus_tasks.create(
            self._db,
            id=task_id,
            task_type=str(task_type),
            compute_tier=str(tier),
            priority=effective_priority,
            drive_alignment=drive_alignment,
            payload=payload,
            created_at=self._clock().isoformat(),
            not_before=not_before,
        )
        return task_id

    async def next_task(self, available_tiers: list[ComputeTier]) -> SurplusTask | None:
        """Return the highest-priority pending task matching available tiers."""
        tier_strs = [str(t) for t in available_tiers]
        row = await surplus_tasks.next_task(self._db, available_tiers=tier_strs)
        if row is None:
            return None
        return SurplusTask(
            id=row["id"],
            task_type=TaskType(row["task_type"]),
            compute_tier=ComputeTier(row["compute_tier"]),
            priority=row["priority"],
            drive_alignment=row["drive_alignment"],
            status=TaskStatus(row["status"]),
            created_at=row["created_at"],
            payload=row["payload"],
            attempt_count=row["attempt_count"],
        )

    async def mark_running(self, task_id: str) -> None:
        await surplus_tasks.mark_running(
            self._db, task_id, started_at=self._clock().isoformat(),
        )

    async def mark_completed(self, task_id: str, staging_id: str | None = None) -> None:
        await surplus_tasks.mark_completed(
            self._db, task_id,
            completed_at=self._clock().isoformat(),
            result_staging_id=staging_id,
        )

    async def mark_failed(self, task_id: str, reason: str) -> None:
        await surplus_tasks.mark_failed(self._db, task_id, failure_reason=reason)

    async def drain_expired(self, *, max_age_hours: int = 72) -> int:
        cutoff = self._clock() - timedelta(hours=max_age_hours)
        return await surplus_tasks.drain_expired(self._db, before=cutoff.isoformat())

    async def recover_stuck(self, *, older_than_hours: int = 2, max_retries: int = 3) -> tuple[int, int]:
        """Recover tasks stuck in 'running' state."""
        return await surplus_tasks.recover_stuck_with_retries(
            self._db, older_than_hours=older_than_hours, max_retries=max_retries,
        )

    async def pending_count(self) -> int:
        return await surplus_tasks.count_pending(self._db)

    async def pending_by_type(self, task_type: TaskType | str) -> int:
        return await surplus_tasks.count_pending_by_type(self._db, str(task_type))

    async def _apply_drive_weight(self, base_priority: float, drive: str) -> float:
        """Multiply base priority by the drive's current weight."""
        cursor = await self._db.execute(
            "SELECT current_weight FROM drive_weights WHERE drive_name = ?",
            (drive,),
        )
        row = await cursor.fetchone()
        weight = row[0] if row else 0.25
        return base_priority * weight
