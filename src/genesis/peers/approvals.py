"""Durable creation intents and retryable notification for exact peer operations.

ApprovalManager remains the approval store. The coordinator persists the hold
and stops the old lease before invoking this service, and alone consumes consent.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from genesis.autonomy.peer_approval import PEER_OPERATION_ACTION_TYPE
from genesis.peers.registry import capability as validate_capability
from genesis.peers.session import PeerSessionBinding
from genesis.peers.tasks import TaskRefusal
from genesis.security.output_scanner import scan_outbound


class PeerApprovals:
    def __init__(self, registry, manager, gate):
        self.registry, self.manager, self.gate = registry, manager, gate
        self._notification_locks = {}

    async def _current(self, db, intent):
        row = await (
            await db.execute(
                "SELECT t.*, (SELECT json_group_object(capability,decision) "
                "FROM peer_grants WHERE peer_id=t.peer_id) AS _current_grants "
                "FROM peer_tasks t JOIN peers p ON p.peer_id=t.peer_id "
                "WHERE t.id=? AND t.generation=? AND t.cancel_requested=0 "
                "AND t.state='working' AND p.active=1 AND p.epoch=t.epoch",
                (intent["task_id"], intent["generation"]),
            )
        ).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            raise TaskRefusal("state_conflict", 409)
        record = dict(row)
        grants = json.loads(record.pop("_current_grants"))
        snapshot = json.loads(row["grants_json"])
        if (
            any(
                snapshot.get(key) not in {"allow", "ask"} or grants.get(key) not in {"allow", "ask"}
                for key in {"conversation", intent["capability"]}
            )
            or row["work_elapsed_s"] >= row["work_limit_s"]
        ):
            raise TaskRefusal("state_conflict", 409)
        return record

    @staticmethod
    def _context(intent):
        return {
            "kind": PEER_OPERATION_ACTION_TYPE,
            "action_type": PEER_OPERATION_ACTION_TYPE,
            "subsystem": "peer_collaboration",
            "channel": "telegram",
            "action_label": intent["description"],
            "extra": {
                key: intent[key]
                for key in (
                    "task_id",
                    "peer_id",
                    "epoch",
                    "segment_id",
                    "generation",
                    "capability",
                    "operation_digest",
                )
            },
        }

    async def request(self, binding: PeerSessionBinding, capability, operation_digest, description):
        validate_capability(capability)
        if (
            not isinstance(binding, PeerSessionBinding)
            or not isinstance(operation_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", operation_digest)
            or not isinstance(description, str)
            or not 1 <= len(description) <= 200
            or not scan_outbound(description).safe
        ):
            raise ValueError("Peer approval refused")
        intent = {
            "task_id": binding.task_id,
            "generation": binding.generation,
            "capability": capability,
        }
        async with self.registry.transaction() as db:
            task = await self._current(db, intent)
            prior = await (
                await db.execute(
                    "SELECT * FROM peer_operation_approvals WHERE task_id=? AND generation=? "
                    "AND capability=? AND operation_digest=?",
                    (binding.task_id, binding.generation, capability, operation_digest),
                )
            ).fetchone()
            if prior is None:
                await db.execute(
                    "INSERT INTO peer_operation_approvals(approval_id,task_id,peer_id,epoch,"
                    "segment_id,generation,capability,operation_digest,description,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        binding.task_id,
                        task["peer_id"],
                        task["epoch"],
                        binding.segment.segment_id,
                        binding.generation,
                        capability,
                        operation_digest,
                        description,
                        task["expires_at"],
                    ),
                )
                prior = await (
                    await db.execute(
                        "SELECT * FROM peer_operation_approvals WHERE task_id=? AND generation=? "
                        "AND capability=? AND operation_digest=?",
                        (binding.task_id, binding.generation, capability, operation_digest),
                    )
                ).fetchone()
            intent = dict(prior)
            if (
                intent["segment_id"] != binding.segment.segment_id
                or intent["description"] != description
            ):
                raise TaskRefusal("state_conflict", 409)
        await self.deliver(intent["approval_id"])
        return intent["approval_id"]

    async def deliver(self, approval_id):
        """Retry only notification/creation; never execute or resolve an operation."""
        async with self._notification_locks.setdefault(approval_id, asyncio.Lock()):
            return await self._deliver(approval_id)

    async def _deliver(self, approval_id):
        async with self.registry.connection() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM peer_operation_approvals WHERE approval_id=?", (approval_id,)
                )
            ).fetchone()
            if row is None:
                return False
            intent = dict(row)
            await self._current(db, intent)
        context = self._context(intent)
        request = await self.manager.get_by_id(approval_id)
        if request is None:
            remaining = int(
                (datetime.fromisoformat(intent["expires_at"]) - datetime.now(UTC)).total_seconds()
            )
            if remaining <= 0:
                return False
            await self.manager.request_approval(
                request_id=approval_id,
                action_type=PEER_OPERATION_ACTION_TYPE,
                action_class="costly_reversible",
                description=intent["description"],
                context=json.dumps(context, sort_keys=True),
                timeout_seconds=remaining,
            )
            request = await self.manager.get_by_id(approval_id)
        if (
            request["action_type"] != PEER_OPERATION_ACTION_TYPE
            or request["description"] != intent["description"]
            or json.loads(request["context"])["extra"] != context["extra"]
        ):
            raise TaskRefusal("state_conflict", 409)
        if request["status"] != "pending" or intent["notified_at"] is not None:
            return False
        delivered = False
        # Notification failure remains visible and retryable, never consent.
        with suppress(Exception):
            delivered = (
                await self.gate._send_request(
                    request_id=approval_id,
                    context=context,
                    action_label=intent["description"],
                    invocation=None,
                    api_error=None,
                )
                is True
            )
        async with self.registry.transaction() as db:
            await db.execute(
                "UPDATE peer_operation_approvals SET notification_attempts=notification_attempts+1,"
                "notified_at=CASE WHEN ? THEN ? ELSE notified_at END,"
                "notification_error=CASE WHEN ? THEN NULL ELSE 'notification_unavailable' END "
                "WHERE approval_id=?",
                (delivered, datetime.now(UTC).isoformat(), delivered, approval_id),
            )
        return delivered

    async def pending(self, identity):
        async with self.registry.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT a.*,r.timeout_at FROM peer_operation_approvals a "
                    "JOIN approval_requests r ON r.id=a.approval_id "
                    "JOIN peer_tasks t ON t.id=a.task_id JOIN peers p ON p.peer_id=t.peer_id "
                    "WHERE a.peer_id=? AND a.epoch=? AND p.active=1 AND p.epoch=a.epoch "
                    "AND t.generation=a.generation AND t.cancel_requested=0 AND t.state='working' "
                    "AND r.status='pending' AND EXISTS(SELECT 1 FROM peer_grants g WHERE "
                    "g.peer_id=p.peer_id AND g.capability='conversation' AND g.decision!='deny')",
                    (identity["peer_id"], identity["epoch"]),
                )
            ).fetchall()
            result = []
            for row in rows:
                try:
                    await self._current(db, row)
                except TaskRefusal:
                    continue
                expiry = min(
                    datetime.fromisoformat(row["expires_at"]),
                    datetime.fromisoformat(row["timeout_at"]),
                )
                remaining = int((expiry - datetime.now(UTC)).total_seconds())
                if remaining > 0:
                    result.append(
                        {
                            "approval_id": row["approval_id"],
                            "action_type": PEER_OPERATION_ACTION_TYPE,
                            "description": row["description"],
                            "created_at": row["created_at"],
                            "timeout": remaining,
                            "notification_error": row["notification_error"],
                        }
                    )
        return result
