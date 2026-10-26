"""Private transactional state for peer segments; no model or network work here."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from genesis.autonomy.peer_approval import PEER_OPERATION_ACTION_TYPE, named_human_resolver
from genesis.db.crud import approval_requests
from genesis.db.crud import direct_session_queue as queue
from genesis.peers.tasks import TaskRefusal


def utcnow():
    return datetime.now(UTC)


def decision(row, capability):
    admitted = json.loads(row["grants_json"]).get(capability, "deny")
    current = json.loads(row["current_grants"]).get(capability, "deny")
    if admitted not in {"allow", "ask"} or current not in {"allow", "ask"}:
        raise TaskRefusal("unauthorized", 401)
    return "ask" if "ask" in {admitted, current} else "allow"


def dispatch_digest(row):
    intent = {
        "operation": "dispatch",
        "message": json.loads(row["message_json"]),
        "work_limit_s": row["work_limit_s"],
    }
    return hashlib.sha256(
        json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def approved_intent(approval):
    """Check the original individual human decision, including timely resolution."""
    try:
        context = json.loads(approval["context"])
        return (
            approval["status"] == "approved"
            and approval["request_action_type"] == PEER_OPERATION_ACTION_TYPE
            and approval["request_description"] == approval["description"]
            and named_human_resolver(approval["resolved_by"])
            and approval_requests.classify_resolver(approval["resolved_by"]) == "human"
            and datetime.fromisoformat(approval["resolved_at"])
            < datetime.fromisoformat(approval["timeout_at"])
            and context.get("action_type") == PEER_OPERATION_ACTION_TYPE
            and context.get("extra")
            == {
                key: approval[key]
                for key in (
                    "task_id",
                    "peer_id",
                    "epoch",
                    "segment_id",
                    "generation",
                    "capability",
                    "operation_digest",
                )
            }
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


async def has_consent(db, row, capability, digest):
    if decision(row, capability) == "allow":
        return True
    consent = await (
        await db.execute(
            "SELECT a.*,r.status,r.resolved_by,r.resolved_at,r.timeout_at,r.context,"
            "r.action_type AS request_action_type,r.description AS request_description "
            "FROM peer_task_consents c JOIN peer_operation_approvals a ON a.approval_id=c.approval_id "
            "JOIN approval_requests r ON r.id=c.approval_id WHERE c.task_id=? AND c.peer_id=? "
            "AND c.epoch=? AND c.capability=? AND c.operation_digest=? AND c.valid_until=? "
            "AND a.expires_at=c.valid_until AND r.consumed_at=c.consumed_at "
            "AND a.task_id=c.task_id AND a.peer_id=c.peer_id AND a.epoch=c.epoch "
            "AND a.capability=c.capability AND a.operation_digest=c.operation_digest",
            (row["id"], row["peer_id"], row["epoch"], capability, digest, row["expires_at"]),
        )
    ).fetchone()
    return consent is not None and approved_intent(consent)


async def disclosure_authorized(db, row):
    if not await has_consent(db, row, "conversation", dispatch_digest(row)):
        raise TaskRefusal("unauthorized", 401)


async def current(db, task_id, generation=None, *, execution=True):
    row = await (
        await db.execute(
            "SELECT t.*,p.same_owner,r.hold_reason,r.hold_segment_id,r.hold_capability,"
            "r.hold_digest,r.approval_id,r.park_id,r.safe_error,"
            "(SELECT json_group_object(capability,decision) FROM peer_grants "
            "WHERE peer_id=t.peer_id) AS current_grants FROM peer_tasks t "
            "JOIN peers p ON p.peer_id=t.peer_id LEFT JOIN peer_task_runtime r ON r.task_id=t.id "
            "WHERE t.id=? AND p.active=1 AND p.epoch=t.epoch",
            (task_id,),
        )
    ).fetchone()
    if (
        row is None
        or not row["same_owner"]  # Cross-owner admission requires unit12 signed evidence.
        or row["cancel_requested"]
        or row["state"]
        not in (
            {"submitted", "working", "input_required"}
            if execution
            else {"submitted", "working", "input_required", "completed"}
        )
        or (generation is not None and row["generation"] != generation)
        or datetime.fromisoformat(row["expires_at"]) <= utcnow()
        or (execution and row["work_elapsed_s"] >= row["work_limit_s"])
    ):
        raise TaskRefusal("state_conflict", 409)
    result = dict(row)
    decision(result, "conversation")
    return result


async def bound(db, binding):
    row = await current(db, binding.task_id, binding.generation)
    segment = await (
        await db.execute(
            "SELECT * FROM peer_segments WHERE id=? AND task_id=? AND generation=?",
            (binding.segment.segment_id, binding.task_id, binding.generation),
        )
    ).fetchone()
    if (
        segment is None
        or segment["status"] not in {"prepared", "running"}
        or segment["working_dir"] != binding.working_dir
        or segment["deadline_at"] != binding.segment.deadline_at
        or segment["deadline_at"] <= time.time()
        or row["hold_reason"] is not None
    ):
        raise TaskRefusal("state_conflict", 409)
    return row


class PeerLifecycleState:
    def __init__(self, registry, directory):
        self.registry, self.directory = registry, Path(directory)

    async def claim(self):
        """Reserve the entire remaining allowance before handing work to the runner."""
        async with self.registry.transaction() as db:
            row = await self._claimable(db)
            if row is None:
                return None
            segment_id = uuid4().hex
            reserved = row["work_limit_s"] - row["work_elapsed_s"]
            deadline = min(
                datetime.fromisoformat(row["expires_at"]).timestamp(), time.time() + reserved
            )
            directory = str(self.directory / segment_id)
            await db.execute(
                "INSERT INTO peer_task_runtime(task_id) VALUES(?) ON CONFLICT DO NOTHING",
                (row["id"],),
            )
            await db.execute(
                "INSERT INTO peer_segments(id,task_id,queue_id,generation,working_dir,deadline_at,"
                "reserved_s,status) VALUES(?,?,?,?,?,?,?,'prepared')",
                (
                    segment_id,
                    row["id"],
                    row["queue_id"],
                    row["generation"],
                    directory,
                    deadline,
                    reserved,
                ),
            )
            await db.execute(
                "UPDATE direct_session_queue SET status='claimed',claimed_at=? WHERE id=?",
                (utcnow().isoformat(), row["queue_id"]),
            )
            await db.execute(
                "UPDATE peer_tasks SET state='working',updated_at=? WHERE id=?",
                (utcnow().isoformat(), row["id"]),
            )
            return row | {
                "segment_id": segment_id,
                "working_dir": directory,
                "deadline_at": deadline,
                "reserved_s": reserved,
            }

    async def _claimable(self, db):
        after_time = after_id = None
        while True:
            selected = await (
                await db.execute(
                    "SELECT t.*,r.hold_reason FROM peer_tasks t "
                    "JOIN direct_session_queue q ON q.id=t.queue_id "
                    "LEFT JOIN peer_task_runtime r ON r.task_id=t.id "
                    "WHERE t.state='submitted' AND q.status='pending' "
                    "AND (? IS NULL OR (t.created_at,t.id)>(?,?)) "
                    "ORDER BY t.created_at,t.id LIMIT 1",
                    (after_time, after_time, after_id),
                )
            ).fetchone()
            if selected is None:
                return None
            after_time, after_id = selected["created_at"], selected["id"]
            # Check raw state first: withdrawn authority cannot be loaded
            # through current(), and physical drain alone is insufficient.
            if selected["hold_reason"] is not None or not await self._drained(db, selected["id"]):
                continue
            try:
                row = await current(db, selected["id"])
            except TaskRefusal:
                await self._end(
                    db,
                    selected["id"],
                    "canceled" if selected["cancel_requested"] else "failed",
                    "Peer task permission unavailable",
                    generation=selected["generation"],
                    pending=True,
                )
                continue
            return row

    async def begin(self, binding, session_id):
        async with self.registry.transaction() as db:
            row = await bound(db, binding)
            await disclosure_authorized(db, row)
            changed = await db.execute(
                "UPDATE peer_segments SET session_id=?,status='running',started_at=? "
                "WHERE id=? AND task_id=? AND generation=? AND status='prepared' AND deadline_at>?",
                (
                    session_id,
                    utcnow().isoformat(),
                    binding.segment.segment_id,
                    binding.task_id,
                    binding.generation,
                    time.time(),
                ),
            )
            if changed.rowcount != 1:
                raise TaskRefusal("state_conflict", 409)
            await db.execute(
                "UPDATE direct_session_queue SET status='dispatched',session_id=?,dispatched_at=? "
                "WHERE id=? AND status='claimed'",
                (session_id, utcnow().isoformat(), row["queue_id"]),
            )

    async def associate_approval(self, binding, approval_id):
        async with self.registry.transaction() as db:
            row = await current(db, binding.task_id, binding.generation)
            intent = await (
                await db.execute(
                    "SELECT * FROM peer_operation_approvals WHERE approval_id=?",
                    (approval_id,),
                )
            ).fetchone()
            if (
                row["hold_reason"] != "approval"
                or intent is None
                or any(intent[key] != row[key] for key in ("peer_id", "epoch", "generation"))
                or intent["task_id"] != row["id"]
                or intent["segment_id"] != row["hold_segment_id"]
                or intent["capability"] != row["hold_capability"]
                or intent["operation_digest"] != row["hold_digest"]
            ):
                raise TaskRefusal("state_conflict", 409)
            await db.execute(
                "UPDATE peer_task_runtime SET approval_id=? WHERE task_id=?",
                (approval_id, row["id"]),
            )

    async def end(self, task_id, state, reason, *, generation=None):
        """Only drained or never-claimed tasks may be terminalized."""
        if state not in {"failed", "canceled", "rejected"}:
            raise ValueError("Invalid peer terminal state")
        async with self.registry.transaction() as db:
            return await self._end(db, task_id, state, reason, generation=generation)

    async def _drained(self, db, task_id):
        row = await (
            await db.execute(
                "SELECT NOT EXISTS(SELECT 1 FROM peer_segments WHERE task_id=? "
                "AND status!='drained')",
                (task_id,),
            )
        ).fetchone()
        return bool(row[0])

    async def _quiescent(self, db, task_id):
        if not await self._drained(db, task_id):
            return False
        row = await (
            await db.execute(
                "SELECT NOT EXISTS(SELECT 1 FROM peer_operations "
                "WHERE task_id=? AND immutable_read=0 AND status IN ('executing','unknown'))",
                (task_id,),
            )
        ).fetchone()
        return bool(row[0])

    async def _end(self, db, task_id, state, reason, *, generation, pending=False):
        """Transaction-local retirement; callbacks never clear unknown effects."""
        row = await (
            await db.execute(
                "SELECT t.*,r.hold_reason,q.status AS queue_status FROM peer_tasks t "
                "LEFT JOIN peer_task_runtime r ON r.task_id=t.id "
                "LEFT JOIN direct_session_queue q ON q.id=t.queue_id WHERE t.id=?",
                (task_id,),
            )
        ).fetchone()
        if (
            row is None
            or row["state"] in {"completed", "failed", "canceled", "rejected"}
            or (generation is not None and row["generation"] != generation)
            or not await self._quiescent(db, task_id)
        ):
            return False
        attempted = await (
            await db.execute("SELECT 1 FROM peer_segments WHERE task_id=? LIMIT 1", (task_id,))
        ).fetchone()
        if (
            generation is None
            and attempted
            and not (state == "canceled" and row["cancel_requested"])
        ):
            return False
        if pending and (
            row["state"] != "submitted"
            or row["queue_status"] != "pending"
            or row["hold_reason"] is not None
        ):
            return False
        stamp = utcnow().isoformat()
        await db.execute(
            "UPDATE peer_tasks SET state=?,slot_reserved=0,generation=generation+1,updated_at=? WHERE id=?",
            (state, stamp, task_id),
        )
        await db.execute(
            "UPDATE direct_session_queue SET status='failed',error_message='Peer task stopped' "
            "WHERE id=? AND status IN ('pending','claimed')",
            (row["queue_id"],),
        )
        await db.execute(
            "INSERT INTO peer_task_runtime(task_id,safe_error) VALUES(?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET safe_error=excluded.safe_error,hold_reason=NULL",
            (task_id, reason),
        )
        return True

    async def hold(self, binding, reason, *, capability=None, digest=None, park_id=None):
        async with self.registry.transaction() as db:
            row = await bound(db, binding)
            if row["hold_reason"] is not None or reason not in {"approval", "provider"}:
                raise TaskRefusal("state_conflict", 409)
            await db.execute(
                "UPDATE peer_tasks SET generation=generation+1,updated_at=? WHERE id=?",
                (utcnow().isoformat(), row["id"]),
            )
            await db.execute(
                "UPDATE peer_task_runtime SET hold_reason=?,hold_segment_id=?,hold_capability=?,"
                "hold_digest=?,approval_id=NULL,park_id=? WHERE task_id=?",
                (reason, binding.segment.segment_id, capability, digest, park_id, row["id"]),
            )
            return row["generation"] + 1

    async def settle(self, binding, elapsed, *, clean, uncertain=False):
        """Charge once; uncertain crash allowance is never refunded."""
        async with self.registry.transaction() as db:
            segment = await (
                await db.execute(
                    "SELECT * FROM peer_segments WHERE id=? AND task_id=? AND generation=?",
                    (binding.segment.segment_id, binding.task_id, binding.generation),
                )
            ).fetchone()
            if segment is None:
                raise TaskRefusal("state_conflict", 409)
            if segment["status"] == "drained":
                return
            if not math.isfinite(elapsed) or elapsed < 0:
                uncertain = True
            charge = (
                segment["reserved_s"]
                if uncertain
                else min(segment["reserved_s"], math.ceil(elapsed))
            )
            charge = max(segment["charged_s"], charge)
            await db.execute(
                "UPDATE peer_tasks SET work_elapsed_s=work_elapsed_s+?,updated_at=? WHERE id=?",
                (charge - segment["charged_s"], utcnow().isoformat(), binding.task_id),
            )
            await db.execute(
                "UPDATE peer_segments SET charged_s=?,status=?,ended_at=? WHERE id=?",
                (charge, "drained" if clean else "blocked", utcnow().isoformat(), segment["id"]),
            )
            if segment["session_id"] is None:
                await db.execute(
                    "UPDATE direct_session_queue SET status='failed',error_message='Peer segment did not start' "
                    "WHERE id=? AND status='claimed'",
                    (segment["queue_id"],),
                )
            if clean:
                await db.execute(
                    "UPDATE peer_tasks SET slot_reserved=0 WHERE id=?", (binding.task_id,)
                )
            else:
                await db.execute(
                    "UPDATE peer_task_runtime SET hold_reason='reconciliation',"
                    "safe_error='Cleanup requires owner reconciliation' WHERE task_id=?",
                    (binding.task_id,),
                )

    async def resume_approval(self, task_id):
        """Named consent consumption and continuation queue insertion commit together."""
        async with self.registry.transaction() as db:
            row = await current(db, task_id)
            if row["hold_reason"] != "approval" or not row["approval_id"]:
                return False
            approval = await (
                await db.execute(
                    "SELECT a.*,r.status,r.resolved_by,r.resolved_at,r.timeout_at,r.consumed_at,r.context,"
                    "r.action_type AS request_action_type,r.description AS request_description "
                    "FROM peer_operation_approvals a JOIN approval_requests r ON r.id=a.approval_id "
                    "JOIN peer_segments s ON s.id=a.segment_id WHERE a.approval_id=? AND s.status='drained'",
                    (row["approval_id"],),
                )
            ).fetchone()
            if approval is None or approval["status"] != "approved":
                return False
            if (
                approval["task_id"] != task_id
                or not approved_intent(approval)
                or approval["peer_id"] != row["peer_id"]
                or approval["epoch"] != row["epoch"]
                or approval["generation"] != row["generation"]
                or approval["segment_id"] != row["hold_segment_id"]
                or approval["capability"] != row["hold_capability"]
                or approval["operation_digest"] != row["hold_digest"]
                or approval["consumed_at"] is not None
                or approval["expires_at"] != row["expires_at"]
            ):
                raise TaskRefusal("state_conflict", 409)
            decision(row, approval["capability"])
            counts = await (
                await db.execute(
                    "SELECT COALESCE(SUM(slot_reserved),0),COALESCE(SUM(CASE WHEN peer_id=? "
                    "THEN slot_reserved ELSE 0 END),0) FROM peer_tasks",
                    (row["peer_id"],),
                )
            ).fetchone()
            if counts[0] >= 2 or counts[1] >= 2:
                return False
            prepared = queue.prepare({"peer_task_id": task_id, "source_tag": "peer_api"})
            await queue.insert_prepared(db, prepared)
            generation = row["generation"] + 1
            stamp = utcnow().isoformat()
            consumed = await db.execute(
                "UPDATE approval_requests SET consumed_at=? WHERE id=? AND status='approved' AND consumed_at IS NULL",
                (stamp, approval["approval_id"]),
            )
            if consumed.rowcount != 1:
                raise TaskRefusal("state_conflict", 409)
            await db.execute(
                "INSERT INTO peer_task_consents(approval_id,task_id,peer_id,epoch,capability,"
                "operation_digest,consumed_at,valid_until) VALUES(?,?,?,?,?,?,?,?)",
                (
                    approval["approval_id"],
                    task_id,
                    row["peer_id"],
                    row["epoch"],
                    approval["capability"],
                    approval["operation_digest"],
                    stamp,
                    row["expires_at"],
                ),
            )
            await db.execute(
                "UPDATE peer_tasks SET generation=?,state='submitted',slot_reserved=1,queue_id=?,updated_at=? WHERE id=?",
                (generation, prepared.id, stamp, task_id),
            )
            await db.execute(
                "UPDATE peer_task_runtime SET hold_reason=NULL,approval_id=NULL WHERE task_id=?",
                (task_id,),
            )
            return True

    async def consent(self, binding, capability, digest):
        """Authorization is reusable; execution outcomes are tracked separately."""
        async with self.registry.transaction() as db:
            row = await bound(db, binding)
            if capability == "conversation" and digest != dispatch_digest(row):
                return False
            if capability != "conversation":
                await disclosure_authorized(db, row)
            return await has_consent(db, row, capability, digest)

    async def record_completion(self, binding, completed_at, execution_elapsed_s):
        """Record invocation return separately from potentially slow cleanup."""
        if (
            not math.isfinite(completed_at)
            or not math.isfinite(execution_elapsed_s)
            or execution_elapsed_s < 0
        ):
            raise ValueError("Invalid peer completion proof")
        async with self.registry.transaction() as db:
            changed = await db.execute(
                "UPDATE peer_segments SET completed_at=?,execution_elapsed_s=? "
                "WHERE id=? AND task_id=? AND generation=? AND status='running' "
                "AND completed_at IS NULL AND deadline_at>=? AND reserved_s>=?",
                (
                    completed_at,
                    execution_elapsed_s,
                    binding.segment.segment_id,
                    binding.task_id,
                    binding.generation,
                    completed_at,
                    execution_elapsed_s,
                ),
            )
            if changed.rowcount != 1:
                raise TaskRefusal("state_conflict", 409)
