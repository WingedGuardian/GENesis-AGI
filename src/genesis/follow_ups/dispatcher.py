"""Follow-up dispatcher — processes pending follow-ups on a schedule.

Runs as an always-on job in the surplus scheduler (NOT gated on idle).
Scheduled follow-ups are committed work, not surplus compute.

Responsibilities:
1. Dispatch scheduled_task follow-ups whose time has arrived
2. Dispatch surplus_task follow-ups immediately
3. Track linked surplus tasks through to completion
4. Mark failed follow-ups for ego evaluation
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiosqlite

from genesis.db.crud import follow_ups as follow_up_crud
from genesis.db.crud import surplus_tasks as surplus_task_crud

if TYPE_CHECKING:
    from genesis.surplus.queue import SurplusQueue

logger = logging.getLogger(__name__)


class FollowUpDispatcher:
    """Processes follow-ups each cycle, dispatching work and tracking completion."""

    def __init__(self, db: aiosqlite.Connection, queue: SurplusQueue):
        self._db = db
        self._queue = queue

    async def run_cycle(self) -> dict:
        """Run one dispatch cycle. Returns summary of actions taken."""
        summary = {
            "scheduled_dispatched": 0,
            "surplus_dispatched": 0,
            "completions_tracked": 0,
            "failures_detected": 0,
        }

        # 1. Dispatch scheduled follow-ups whose time has arrived
        due = await follow_up_crud.get_scheduled_due(self._db)
        for fu in due:
            try:
                task_id = await self._dispatch_to_surplus(fu)
                if task_id:
                    await follow_up_crud.link_task(self._db, fu["id"], task_id)
                    summary["scheduled_dispatched"] += 1
                    logger.info(
                        "Follow-up %s dispatched as surplus task %s (scheduled)",
                        fu["id"][:8], task_id[:8],
                    )
            except Exception:
                logger.exception("Failed to dispatch scheduled follow-up %s", fu["id"][:8])

        # 2. Dispatch surplus_task follow-ups immediately
        surplus_pending = await follow_up_crud.get_pending(
            self._db, strategy="surplus_task",
        )
        for fu in surplus_pending:
            try:
                task_id = await self._dispatch_to_surplus(fu)
                if task_id:
                    await follow_up_crud.link_task(self._db, fu["id"], task_id)
                    summary["surplus_dispatched"] += 1
                    logger.info(
                        "Follow-up %s dispatched as surplus task %s (immediate)",
                        fu["id"][:8], task_id[:8],
                    )
            except Exception:
                logger.exception("Failed to dispatch surplus follow-up %s", fu["id"][:8])

        # 3. Track linked surplus tasks
        linked = await follow_up_crud.get_linked_active(self._db)
        for fu in linked:
            try:
                await self._track_linked_task(fu, summary)
            except Exception:
                logger.exception("Failed to track follow-up %s", fu["id"][:8])

        if any(v > 0 for v in summary.values()):
            logger.info("Follow-up dispatch cycle: %s", summary)

        return summary

    async def _dispatch_to_surplus(self, fu: dict) -> str | None:
        """Create a surplus task from a follow-up. Returns task ID or None."""
        # Parse task type and tier from follow-up content/payload
        task_type, tier, payload = self._parse_follow_up_task(fu)

        # Enforce per-type cap to prevent queue flooding (e.g. MODEL_EVAL max 10)
        from genesis.surplus.types import TaskType

        _TYPE_CAPS = {TaskType.MODEL_EVAL: 10}
        cap = _TYPE_CAPS.get(task_type)
        if cap is not None:
            pending = await self._queue.pending_by_type(task_type)
            if pending >= cap:
                logger.info(
                    "Skipping follow-up %s: %s queue already has %d pending (cap %d)",
                    fu["id"][:8], task_type, pending, cap,
                )
                return None

        task_id = await self._queue.enqueue(
            task_type,
            tier,
            priority=self._priority_to_float(fu.get("priority", "medium")),
            drive_alignment="cooperation",
            payload=json.dumps(payload) if payload else None,
            not_before=fu.get("scheduled_at"),
        )

        # Mark follow-up as in_progress
        await follow_up_crud.update_status(self._db, fu["id"], "in_progress")

        return task_id

    def _parse_follow_up_task(self, fu: dict) -> tuple:
        """Determine surplus task type and tier from follow-up content.

        Returns (task_type, compute_tier, payload_dict).

        First tries structured routing: if the follow-up's reason field contains
        JSON with a ``task_type`` key, uses that directly (e.g. recon-originated
        follow-ups). Falls back to keyword matching on content.
        """
        from genesis.surplus.types import ComputeTier, TaskType

        _TIER_MAP = {
            "free_api": ComputeTier.FREE_API,
            "cheap_paid": ComputeTier.CHEAP_PAID,
            "local_30b": ComputeTier.LOCAL_30B,
        }
        _TYPE_MAP = {t.value: t for t in TaskType}

        # 1. Try structured payload in reason field
        reason = fu.get("reason") or ""
        if reason.startswith("{"):
            try:
                parsed = json.loads(reason)
                if isinstance(parsed, dict) and "task_type" in parsed:
                    task_type = _TYPE_MAP.get(parsed["task_type"])
                    tier = _TIER_MAP.get(parsed.get("compute_tier", ""), ComputeTier.FREE_API)
                    if task_type is not None:
                        payload = parsed.get("payload", {})
                        if "source" in payload:
                            payload["original_source"] = payload["source"]
                        payload["source"] = "follow_up"
                        payload["follow_up_id"] = fu["id"]
                        return task_type, tier, payload
            except (json.JSONDecodeError, KeyError):
                pass

        # 2. Fall back to keyword matching on content
        content = fu.get("content", "").lower()

        if "benchmark" in content or "eval" in content:
            return TaskType.MODEL_EVAL, ComputeTier.FREE_API, {"source": "follow_up", "follow_up_id": fu["id"]}
        if "research" in content:
            return TaskType.ANTICIPATORY_RESEARCH, ComputeTier.FREE_API, {"source": "follow_up", "follow_up_id": fu["id"]}
        if "brainstorm" in content:
            return TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, {"source": "follow_up", "follow_up_id": fu["id"]}
        if "cleanup" in content or "disk" in content:
            return TaskType.DISK_CLEANUP, ComputeTier.FREE_API, {"source": "follow_up", "follow_up_id": fu["id"]}

        # Default: brainstorm (safe, always-available task type)
        return TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, {
            "source": "follow_up",
            "follow_up_id": fu["id"],
            "original_content": fu.get("content", ""),
        }

    @staticmethod
    def _priority_to_float(priority: str) -> float:
        return {"critical": 0.9, "high": 0.7, "medium": 0.5, "low": 0.3}.get(priority, 0.5)

    async def _track_linked_task(self, fu: dict, summary: dict) -> None:
        """Check if linked surplus task completed or failed."""
        task_id = fu.get("linked_task_id")
        if not task_id:
            return

        task = await surplus_task_crud.get_by_id(self._db, task_id)
        if task is None:
            # Task deleted — mark follow-up failed
            await follow_up_crud.update_status(
                self._db, fu["id"], "failed",
                blocked_reason="Linked surplus task was deleted",
            )
            summary["failures_detected"] += 1
            return

        status = task.get("status", "")
        if status == "completed":
            await follow_up_crud.update_status(
                self._db, fu["id"], "completed",
                resolution_notes=f"Surplus task {task_id[:8]} completed successfully",
            )
            summary["completions_tracked"] += 1
            logger.info("Follow-up %s completed via surplus task %s", fu["id"][:8], task_id[:8])
        elif status == "failed":
            reason = task.get("failure_reason", "unknown")
            await follow_up_crud.update_status(
                self._db, fu["id"], "failed",
                blocked_reason=f"Surplus task failed: {reason}",
            )
            summary["failures_detected"] += 1
            logger.warning("Follow-up %s failed — surplus task %s: %s", fu["id"][:8], task_id[:8], reason)
        elif status == "cancelled":
            await follow_up_crud.update_status(
                self._db, fu["id"], "failed",
                blocked_reason="Linked surplus task was cancelled",
            )
            summary["failures_detected"] += 1
        # else: still pending/running — do nothing, check next cycle
