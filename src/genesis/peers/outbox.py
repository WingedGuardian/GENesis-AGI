"""Addressed, retained A2A messages awaiting peer collection, not execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from a2a.types import Message, Part, Role
from google.protobuf.json_format import MessageToDict, ParseDict

from genesis.peers.auth import PeerIdentity
from genesis.peers.conversation import OWNED, ConversationRefusal, authorize, uuid_id
from genesis.peers.disclosure_scan import json_strings_safe
from genesis.peers.protocol import _unique_object
from genesis.peers.registry import PeerRegistry, identifier


@dataclass(frozen=True)
class PeerEnvelope:
    peer_id: str
    epoch: str
    message_json: str

    @property
    def message(self):
        return json.loads(self.message_json)

    @property
    def message_id(self):
        return self.message["messageId"]

    @property
    def context_id(self):
        return self.message["contextId"]

    def encode(self):
        return json.dumps(
            {"peer_id": self.peer_id, "epoch": self.epoch, "message": self.message},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def decode(cls, text):
        try:
            if len(text.encode("utf-8")) > 256 * 1024:
                raise ValueError
            value = json.loads(text, object_pairs_hook=_unique_object)
            if set(value) != {"peer_id", "epoch", "message"}:
                raise ValueError
            identifier(value["peer_id"])
            if not isinstance(value["epoch"], str) or len(value["epoch"]) != 32:
                raise ValueError
            int(value["epoch"], 16)
            msg = ParseDict(value["message"], Message(), ignore_unknown_fields=False)
            if (
                msg.role != Role.ROLE_AGENT
                or len(msg.parts) != 1
                or msg.parts[0].WhichOneof("content") != "text"
                or not msg.parts[0].text.strip()
                or msg.task_id
                or msg.reference_task_ids
                or msg.extensions
                or msg.metadata
                or msg.parts[0].filename
                or msg.parts[0].metadata
                or msg.parts[0].media_type
            ):
                raise ValueError
            uuid_id(msg.message_id)
            uuid_id(msg.context_id)
            if not json_strings_safe(value["message"]):
                raise ValueError
            return cls(
                value["peer_id"],
                value["epoch"],
                json.dumps(MessageToDict(msg), sort_keys=True, separators=(",", ":")),
            )
        except Exception:
            raise ConversationRefusal("outbound_refused", 409) from None

    def request(self, *, defer_retry=True):
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        topic = hashlib.sha256(
            f"{self.peer_id}:{self.epoch}:{self.message_id}".encode()
        ).hexdigest()
        return OutreachRequest(
            category=OutreachCategory.NOTIFICATION,
            topic=topic,
            context=self.message["parts"][0]["text"],
            salience_score=1,
            signal_type="peer_message",
            channel="agent",
            validated_recipient=self.peer_id,
            verbatim=True,
            defer_retry=defer_retry,
            peer_envelope=self,
        )


async def registry_for_db(db):
    rows = await (await db.execute("PRAGMA database_list")).fetchall()
    path = next((row[2] for row in rows if row[1] == "main"), "")
    if not path:
        raise ConversationRefusal("not_ready", 503)
    return PeerRegistry(path)


async def enabled(db):
    row = await (
        await db.execute(
            "SELECT 1 FROM peer_settings WHERE id=1 AND mode IN ('fallback','sam') "
            "AND service_url IS NOT NULL AND service_url!=''"
        )
    ).fetchone()
    if row is None:
        raise ConversationRefusal("not_configured", 503)


async def owned_context(db, peer_id, epoch, context_id):
    row = await (
        await db.execute(
            f"SELECT 1 FROM cc_sessions WHERE {OWNED} AND json_extract(metadata,'$.context_id')=? "  # noqa: S608 — compiled owner clause only
            "UNION ALL SELECT 1 FROM peer_outbox WHERE peer_id=? AND epoch=? AND context_id=? LIMIT 1",  # noqa: S608 — compiled owner clause only
            (peer_id, epoch, context_id, peer_id, epoch, context_id),
        )
    ).fetchone()
    return row is not None


class Outbox:
    def __init__(self, registry):
        self.registry = registry

    async def prepare(self, peer_id, text, context_id=None, message_id=None):
        identifier(peer_id)
        if not isinstance(text, str) or not text.strip():
            raise ConversationRefusal("validation_error", 400)
        message_id = uuid_id(message_id) if message_id is not None else str(uuid4())
        async with self.registry.transaction() as db:
            await enabled(db)
            peer = await (
                await db.execute("SELECT * FROM peers WHERE peer_id=?", (peer_id,))
            ).fetchone()
            if peer is None:
                raise ConversationRefusal("not_found", 404)
            peer = dict(peer)
            await authorize(db, PeerIdentity("", peer))
            default = str(uuid5(UUID(message_id), f"{peer_id}:{peer['epoch']}:context"))
            context_id = uuid_id(context_id) if context_id is not None else default
            if context_id != default and not await owned_context(
                db, peer_id, peer["epoch"], context_id
            ):
                raise ConversationRefusal("not_found", 404)
            msg = Message(
                message_id=message_id,
                context_id=context_id,
                role=Role.ROLE_AGENT,
                parts=[Part(text=text)],
            )
            envelope = PeerEnvelope(peer_id, peer["epoch"], json.dumps(MessageToDict(msg)))
            envelope = PeerEnvelope.decode(envelope.encode())
            existing = await self._existing(db, envelope)
            return envelope, existing

    async def _existing(self, db, envelope):
        row = await (
            await db.execute(
                "SELECT * FROM peer_outbox WHERE peer_id=? AND epoch=? AND message_id=?",
                (envelope.peer_id, envelope.epoch, envelope.message_id),
            )
        ).fetchone()
        if row is not None and row["message"] != envelope.message_json:
            raise ConversationRefusal("state_conflict", 409)
        return dict(row) if row else None

    async def put(self, envelope):
        envelope = PeerEnvelope.decode(envelope.encode())
        async with self.registry.transaction() as db:
            await enabled(db)
            await authorize(
                db, PeerIdentity("", {"peer_id": envelope.peer_id, "epoch": envelope.epoch})
            )
            existing = await self._existing(db, envelope)
            if existing:
                return envelope.message_id
            pending_id = str(
                uuid5(UUID(envelope.message_id), f"{envelope.peer_id}:{envelope.epoch}:pending")
            )
            pending = await (
                await db.execute("SELECT * FROM pending_outreach WHERE id=?", (pending_id,))
            ).fetchone()
            if pending is not None:
                if pending["channel"] != "agent" or pending["message"] != envelope.encode():
                    raise ConversationRefusal("state_conflict", 409)
                if pending["delivered"] or pending["cancelled_at"]:
                    raise ConversationRefusal("state_conflict", 409)
            default = str(
                uuid5(UUID(envelope.message_id), f"{envelope.peer_id}:{envelope.epoch}:context")
            )
            if envelope.context_id != default and not await owned_context(
                db, envelope.peer_id, envelope.epoch, envelope.context_id
            ):
                raise ConversationRefusal("not_found", 404)
            await db.execute(
                "INSERT INTO peer_outbox(peer_id,epoch,message_id,context_id,message,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    envelope.peer_id,
                    envelope.epoch,
                    envelope.message_id,
                    envelope.context_id,
                    envelope.message_json,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return envelope.message_id

    async def enqueue(self, envelope):
        from genesis.db.crud import pending_outreach

        envelope = PeerEnvelope.decode(envelope.encode())
        pending_id = str(
            uuid5(UUID(envelope.message_id), f"{envelope.peer_id}:{envelope.epoch}:pending")
        )
        async with self.registry.transaction() as db:
            await enabled(db)
            await authorize(
                db, PeerIdentity("", {"peer_id": envelope.peer_id, "epoch": envelope.epoch})
            )
            if await self._existing(db, envelope):
                return "", "queued_for_collection"
            row = await (
                await db.execute(
                    "SELECT * FROM pending_outreach WHERE id=?",
                    (pending_id,),
                )
            ).fetchone()
            if row is not None:
                if row["channel"] != "agent" or row["message"] != envelope.encode():
                    raise ConversationRefusal("state_conflict", 409)
                state = (
                    "queue_closed"
                    if row["delivered"] or row["cancelled_at"]
                    else "queued_for_governance"
                )
            else:
                await pending_outreach.enqueue(
                    db,
                    message=envelope.encode(),
                    category="notification",
                    channel="agent",
                    pending_id=pending_id,
                    commit=False,
                )
                state = "queued_for_governance"
        return pending_id, state

    async def poll(self, identity, size=20, token=None):
        if type(size) is not int or not 1 <= size <= 100:
            raise ConversationRefusal("validation_error", 400)
        peer = identity.peer
        async with self.registry.connection() as db:
            await db.execute("BEGIN")
            await enabled(db)
            await authorize(db, identity)
            values = (peer["peer_id"], peer["epoch"])
            anchor = None
            if token:
                uuid_id(token)
                anchor = await (
                    await db.execute(
                        "SELECT created_at,message_id FROM peer_outbox WHERE peer_id=? AND epoch=? AND message_id=?",
                        (*values, token),
                    )
                ).fetchone()
                if anchor is None:
                    raise ConversationRefusal("not_found", 404)
            clause = " AND (created_at,message_id)>(?,?)" if anchor else ""
            rows = await (
                await db.execute(
                    "SELECT * FROM peer_outbox WHERE peer_id=? AND epoch=? AND acknowledged_at IS NULL"  # noqa: S608 — compiled keyset clause only
                    + clause
                    + " ORDER BY created_at,message_id LIMIT ?",  # noqa: S608 — compiled keyset clause only
                    (*values, *tuple(anchor), size + 1) if anchor else (*values, size + 1),
                )
            ).fetchall()
            messages = []
            for row in rows[:size]:
                envelope = PeerEnvelope.decode(
                    json.dumps(
                        {
                            "peer_id": row["peer_id"],
                            "epoch": row["epoch"],
                            "message": json.loads(row["message"]),
                        }
                    )
                )
                messages.append({"message": envelope.message, "createdAt": row["created_at"]})
            return messages, rows[size - 1]["message_id"] if len(rows) > size else ""

    async def ack(self, identity, message_id):
        uuid_id(message_id)
        async with self.registry.transaction() as db:
            await enabled(db)
            await authorize(db, identity)
            result = await db.execute(
                "UPDATE peer_outbox SET acknowledged_at=coalesce(acknowledged_at,?) "
                "WHERE peer_id=? AND epoch=? AND message_id=?",
                (
                    datetime.now(UTC).isoformat(),
                    identity.peer["peer_id"],
                    identity.peer["epoch"],
                    message_id,
                ),
            )
            if result.rowcount != 1:
                raise ConversationRefusal("not_found", 404)
