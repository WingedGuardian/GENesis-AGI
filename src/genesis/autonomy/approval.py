"""Approval lifecycle manager — creation, timeout, resolution, cancellation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import aiosqlite

from genesis.autonomy.types import ApprovalStatus
from genesis.db.crud import approval_requests as crud
from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)


class ApprovalManager:
    """Gate mechanism for actions that require explicit human approval.

    Key invariant: **no auto-approve path exists.**  Timeout always means
    reject/expire.  Irreversible actions use ``timeout_seconds=None`` (wait
    forever) and therefore never auto-expire.
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        event_bus: object | None = None,
        classifier: object | None = None,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._classifier = classifier

    async def request_approval(
        self,
        *,
        action_type: str,
        action_class: str,
        description: str,
        context: str | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        """Create an approval request and return its ID.

        If *timeout_seconds* is ``None`` and a classifier is available, the
        timeout is looked up via ``classifier.get_timeout(action_type)``.  A
        ``None`` timeout means the request waits indefinitely (irreversible
        actions).
        """
        if timeout_seconds is None and self._classifier is not None:
            timeout_seconds = self._classifier.get_timeout(action_type)

        now = datetime.now(UTC)
        now_iso = now.isoformat()

        # Convert timeout_seconds to absolute timeout_at timestamp
        timeout_at: str | None = None
        if timeout_seconds is not None:
            timeout_at = (now + timedelta(seconds=timeout_seconds)).isoformat()

        request_id = str(uuid4())

        await crud.create(
            self._db,
            id=request_id,
            action_type=action_type,
            action_class=action_class,
            description=description,
            context=context,
            timeout_at=timeout_at,
            created_at=now_iso,
        )

        logger.info(
            "Approval requested: %s (%s / %s) timeout=%s",
            request_id, action_type, action_class, timeout_seconds,
        )

        await self._emit(
            severity=Severity.INFO,
            event_type="approval.requested",
            message=f"Approval requested for {action_type}: {description}",
        )

        return request_id

    async def resolve(
        self,
        request_id: str,
        *,
        status: str,
        resolved_by: str = "user",
    ) -> bool:
        """Resolve a pending request (approved/rejected/expired/cancelled).

        Returns ``False`` if the request was not found or was not pending.
        """
        now_iso = datetime.now(UTC).isoformat()

        ok = await crud.resolve(
            self._db,
            request_id,
            status=status,
            resolved_by=resolved_by,
            resolved_at=now_iso,
        )

        if ok:
            logger.info("Approval %s resolved as %s by %s", request_id, status, resolved_by)
        else:
            logger.warning("Failed to resolve approval %s — not found or not pending", request_id)

        return ok

    async def cancel(self, request_id: str) -> bool:
        """Cancel a pending request."""
        return await self.resolve(
            request_id, status=ApprovalStatus.CANCELLED, resolved_by="system",
        )

    async def expire_timed_out(self) -> int:
        """Expire all timed-out pending requests.  Returns the count expired."""
        now_iso = datetime.now(UTC).isoformat()
        expired = await crud.expire_timed_out(self._db, now=now_iso)

        if expired:
            logger.warning("Expired %d timed-out approval requests", expired)
            await self._emit(
                severity=Severity.WARNING,
                event_type="approval.expired",
                message=f"{expired} approval request(s) expired",
            )

        return expired

    async def get_pending(self) -> list[dict]:
        """Return all pending approval requests."""
        return await crud.list_pending(self._db)

    async def get_recent(self, *, limit: int = 200) -> list[dict]:
        """Return recent approval requests, newest first."""
        return await crud.list_recent(self._db, limit=limit)

    async def get_by_id(self, request_id: str) -> dict | None:
        """Return a single approval request by ID, or ``None``."""
        return await crud.get_by_id(self._db, request_id)

    async def update_context(self, request_id: str, *, context: str) -> bool:
        """Replace the serialized context payload for a request."""
        return await crud.update_context(self._db, request_id, context=context)

    async def _emit(
        self,
        *,
        severity: Severity,
        event_type: str,
        message: str,
    ) -> None:
        """Emit an observability event if an event bus is configured."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit(  # type: ignore[union-attr]
                Subsystem.AUTONOMY, severity, event_type, message,
            )
        except Exception:
            logger.error("Failed to emit event %s", event_type, exc_info=True)
