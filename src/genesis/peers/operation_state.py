"""Private logical operation receipts; no peer-facing executor or authority."""

import json
from uuid import uuid4

from genesis.peers.lifecycle_state import bound, disclosure_authorized, has_consent, utcnow
from genesis.peers.tasks import TaskRefusal
from genesis.security.output_scanner import scan_outbound


async def operation_authorized(db, task, capability, digest):
    await disclosure_authorized(db, task)
    # task_context and resources_list belong to the approved conversation.
    # Their distinct invocation digests identify outcomes, not new consent.
    if capability != "conversation" and not await has_consent(db, task, capability, digest):
        raise TaskRefusal("unauthorized", 401)


class PeerOperationState:
    def __init__(self, registry):
        self.registry = registry

    async def prepare(self, binding, capability, digest, *, immutable_read):
        """Retry only an exact read after the previous attempt's confirmed drain."""
        if type(immutable_read) is not bool:
            raise ValueError("Invalid peer operation type")
        async with self.registry.transaction() as db:
            task = await bound(db, binding)
            await operation_authorized(db, task, capability, digest)
            row = await (
                await db.execute(
                    "SELECT o.*,s.status AS segment_status FROM peer_operations o "
                    "JOIN peer_segments s ON s.id=o.segment_id WHERE o.task_id=? "
                    "AND o.capability=? AND o.operation_digest=?",
                    (binding.task_id, capability, digest),
                )
            ).fetchone()
            if row is None:
                identifier = uuid4().hex
                await db.execute(
                    "INSERT INTO peer_operations(id,task_id,capability,operation_digest,"
                    "immutable_read,segment_id,status,updated_at) VALUES(?,?,?,?,?,?,'prepared',?)",
                    (
                        identifier,
                        binding.task_id,
                        capability,
                        digest,
                        immutable_read,
                        binding.segment.segment_id,
                        utcnow().isoformat(),
                    ),
                )
                return {"id": identifier, "status": "prepared", "result_json": None}
            if row["immutable_read"] != immutable_read:
                raise TaskRefusal("state_conflict", 409)
            if row["status"] == "completed":
                return dict(row)
            if row["segment_id"] == binding.segment.segment_id:
                if row["status"] != "prepared":
                    raise TaskRefusal("state_conflict", 409)
                return dict(row)
            if row["segment_status"] != "drained" or (
                row["status"] != "prepared" and not immutable_read
            ):
                raise TaskRefusal("state_conflict", 409)
            await db.execute(
                "UPDATE peer_operations SET segment_id=?,status='prepared',updated_at=? WHERE id=?",
                (binding.segment.segment_id, utcnow().isoformat(), row["id"]),
            )
            return dict(row) | {"status": "prepared", "segment_id": binding.segment.segment_id}

    async def transition(self, binding, operation_id, status, *, result=None):
        """CAS tracks start, return and uncertain effects without consuming consent."""
        prior = {"executing": "prepared", "completed": "executing", "unknown": "executing"}
        if status not in prior or (status != "completed" and result is not None):
            raise ValueError("Invalid peer operation transition")
        encoded = None
        if status == "completed":
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
            # A receipt can contain the broker's 256KiB immutable resource read.
            # JSON escapes can expand each input byte sixfold. Full artifacts
            # are stored separately and have no limit imposed by this receipt.
            if len(encoded.encode()) > 2 * 1024 * 1024 or not scan_outbound(encoded).safe:
                raise ValueError("Peer operation result refused")
        async with self.registry.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM peer_operations WHERE id=? AND task_id=? AND segment_id=?",
                    (operation_id, binding.task_id, binding.segment.segment_id),
                )
            ).fetchone()
            if row is None:
                raise TaskRefusal("state_conflict", 409)
            if status != "unknown":
                task = await bound(db, binding)
                await operation_authorized(db, task, row["capability"], row["operation_digest"])
            changed = await db.execute(
                "UPDATE peer_operations SET status=?,result_json=?,updated_at=? "
                "WHERE id=? AND status=? AND segment_id=?",
                (
                    status,
                    encoded,
                    utcnow().isoformat(),
                    operation_id,
                    prior[status],
                    binding.segment.segment_id,
                ),
            )
            if changed.rowcount != 1:
                raise TaskRefusal("state_conflict", 409)
