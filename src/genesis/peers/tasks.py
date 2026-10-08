"""Atomic peer receipt/quota/queue admission on registry-owned connections."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from genesis.db.crud import direct_session_queue as queue
from genesis.peers.registry import PeerRegistry

TERMINAL = ("completed", "failed", "canceled", "rejected")


class TaskRefusal(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


class PeerTasks:
    def __init__(self, registry: PeerRegistry):
        self.registry = registry

    async def admit(self, identity: dict, message: dict, *, work_limit_s: int = 3600) -> dict:
        """No model work or network effect occurs under the write transaction."""
        if type(work_limit_s) is not int or not 1 <= work_limit_s <= 7200:
            raise TaskRefusal("validation_error", 400)
        encoded = json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256((encoded + "\n" + str(work_limit_s)).encode()).hexdigest()
        task_id = uuid.uuid4().hex
        context_id = message.get("contextId") or uuid.uuid4().hex
        prepared = queue.prepare({"peer_task_id": task_id, "source_tag": "peer_api"})
        async with self.registry.transaction() as db:
            now = datetime.now(UTC)
            current = await (
                await db.execute(
                    "SELECT * FROM peers WHERE peer_id=? AND epoch=? AND active=1",
                    (identity["peer_id"], identity["epoch"]),
                )
            ).fetchone()
            if current is None:
                raise TaskRefusal("unauthorized", 401)
            old = await (
                await db.execute(
                    "SELECT r.intent_digest,t.* FROM peer_receipts r JOIN peer_tasks t ON t.id=r.task_id "
                    "WHERE r.peer_id=? AND r.epoch=? AND r.message_id=?",
                    (current["peer_id"], current["epoch"], message["messageId"]),
                )
            ).fetchone()
            grants = dict(
                await (
                    await db.execute(
                        "SELECT capability,decision FROM peer_grants WHERE peer_id=?",
                        (current["peer_id"],),
                    )
                ).fetchall()
            )
            if grants.get("conversation", "deny") == "deny":
                raise TaskRefusal("unauthorized", 401)
            if old is not None:
                if old["intent_digest"] != digest:
                    raise TaskRefusal("state_conflict", 409)
                return dict(old)
            if (
                message.get("contextId")
                and not await (
                    await db.execute(
                        "SELECT 1 FROM peer_tasks WHERE peer_id=? AND epoch=? AND context_id=?",
                        (current["peer_id"], current["epoch"], context_id),
                    )
                ).fetchone()
            ):
                raise TaskRefusal("not_found", 404)
            counts = await (
                await db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(slot_reserved),0) FROM peer_tasks "
                    "WHERE peer_id=? AND epoch=? AND state NOT IN ('completed','failed','canceled','rejected')",
                    (current["peer_id"], current["epoch"]),
                )
            ).fetchone()
            slots = (
                await (
                    await db.execute("SELECT COALESCE(SUM(slot_reserved),0) FROM peer_tasks")
                ).fetchone()
            )[0]
            if counts[0] >= 20 or counts[1] >= 2 or slots >= 2:
                raise TaskRefusal("rate_limited", 429)
            day = now.date().isoformat()
            usage = await (
                await db.execute(
                    "SELECT admissions FROM peer_daily_admissions WHERE peer_id=? AND epoch=? AND utc_day=?",
                    (current["peer_id"], current["epoch"], day),
                )
            ).fetchone()
            if usage is not None and usage[0] >= current["daily_allowance"]:
                raise TaskRefusal("rate_limited", 429)
            await db.execute(
                "INSERT INTO peer_tasks(id,peer_id,epoch,context_id,message_json,grants_json,grant_revision,"
                "queue_id,created_at,expires_at,work_limit_s) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    current["peer_id"],
                    current["epoch"],
                    context_id,
                    encoded,
                    json.dumps(grants, sort_keys=True),
                    current["revision"],
                    prepared.id,
                    now.isoformat(),
                    (now + timedelta(hours=24)).isoformat(),
                    work_limit_s,
                ),
            )
            await db.execute(
                "INSERT INTO peer_receipts(peer_id,epoch,message_id,intent_digest,task_id) VALUES(?,?,?,?,?)",
                (current["peer_id"], current["epoch"], message["messageId"], digest, task_id),
            )
            await db.execute(
                "INSERT INTO peer_daily_admissions(peer_id,epoch,utc_day,admissions) VALUES(?,?,?,1) "
                "ON CONFLICT(peer_id,epoch,utc_day) DO UPDATE SET admissions=admissions+1",
                (current["peer_id"], current["epoch"], day),
            )
            await queue.insert_prepared(db, prepared)
            return dict(
                await (
                    await db.execute("SELECT * FROM peer_tasks WHERE id=?", (task_id,))
                ).fetchone()
            )

    async def owned(self, identity: dict, task_id: str, *, db=None) -> dict:
        if db is None:
            async with self.registry.connection() as connection:
                return await self.owned(identity, task_id, db=connection)
        row = await (
            await db.execute(
                "SELECT t.* FROM peer_tasks t JOIN peers p ON p.peer_id=t.peer_id "
                "WHERE t.id=? AND t.peer_id=? AND t.epoch=? AND p.epoch=t.epoch AND p.active=1 "
                "AND EXISTS(SELECT 1 FROM peer_grants g WHERE g.peer_id=p.peer_id "
                "AND g.capability='conversation' AND g.decision!='deny')",
                (task_id, identity["peer_id"], identity["epoch"]),
            )
        ).fetchone()
        if row is None:
            raise TaskRefusal("not_found", 404)
        return dict(row)

    async def page(
        self, identity: dict, *, page_size: int = 20, page_token: str = ""
    ) -> tuple[list[dict], str, int]:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise TaskRefusal("validation_error", 400)
        last_time = last_id = ""
        if page_token:
            try:
                if len(page_token) > 2048:
                    raise ValueError
                bound = json.loads(base64.b64decode(page_token, altchars=b"-_", validate=True))
                if (
                    not isinstance(bound, list)
                    or len(bound) != 4
                    or not all(isinstance(value, str) for value in bound)
                ):
                    raise ValueError
                if bound[:2] != [identity["peer_id"], identity["epoch"]]:
                    raise TaskRefusal("not_found", 404)
                last_time, last_id = bound[2:]
            except TaskRefusal:
                raise
            except Exception:
                raise TaskRefusal("validation_error", 400) from None
        predicate = (
            " FROM peer_tasks t JOIN peers p ON p.peer_id=t.peer_id "
            "WHERE t.peer_id=? AND t.epoch=? AND p.epoch=t.epoch AND p.active=1 "
            "AND EXISTS(SELECT 1 FROM peer_grants g WHERE g.peer_id=p.peer_id "
            "AND g.capability='conversation' AND g.decision!='deny')"
        )
        args = (identity["peer_id"], identity["epoch"])
        async with self.registry.connection() as db:
            total = (await (await db.execute("SELECT COUNT(*)" + predicate, args)).fetchone())[0]
            rows = await (
                await db.execute(
                    "SELECT t.*"
                    + predicate
                    + " AND (t.created_at,t.id)>(?,?) ORDER BY t.created_at,t.id LIMIT ?",
                    (*args, last_time, last_id, page_size + 1),
                )
            ).fetchall()
        next_token = ""
        if len(rows) > page_size:
            last = rows[page_size - 1]
            next_token = base64.urlsafe_b64encode(
                json.dumps([*args, last["created_at"], last["id"]]).encode()
            ).decode()
        return [dict(row) for row in rows[:page_size]], next_token, total

    async def _before_pending_cancel(self, db, row):
        """Internal lifecycle extension; caller owns the pending-cancel transaction."""

    async def cancel(self, identity: dict, task_id: str) -> dict:
        await self.owned(identity, task_id)
        async with self.registry.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT t.*,q.status AS queue_status FROM peer_tasks t JOIN direct_session_queue q ON q.id=t.queue_id "
                    "JOIN peers p ON p.peer_id=t.peer_id WHERE t.id=? AND t.peer_id=? AND t.epoch=? AND p.active=1 AND p.epoch=t.epoch "
                    "AND EXISTS(SELECT 1 FROM peer_grants g WHERE g.peer_id=p.peer_id AND g.capability='conversation' AND g.decision!='deny')",
                    (task_id, identity["peer_id"], identity["epoch"]),
                )
            ).fetchone()
            if row is None:
                raise TaskRefusal("not_found", 404)
            if row["state"] == "canceled":
                return dict(row)
            if row["state"] in TERMINAL:
                raise TaskRefusal("state_conflict", 400)
            if row["state"] == "submitted" and row["queue_status"] == "pending":
                await self._before_pending_cancel(db, row)
                await db.execute(
                    "UPDATE direct_session_queue SET status='failed',error_message='Peer task canceled' WHERE id=?",
                    (row["queue_id"],),
                )
                await db.execute(
                    "UPDATE peer_tasks SET state='canceled',slot_reserved=0,cancel_requested=1,generation=generation+1,updated_at=? WHERE id=?",
                    (datetime.now(UTC).isoformat(), task_id),
                )
            else:
                # A claimed/running segment retains its slot until the future
                # coordinator proves BOTH scope and broker operations drained.
                await db.execute(
                    "UPDATE peer_tasks SET cancel_requested=1,generation=generation+1,updated_at=? WHERE id=? AND cancel_requested=0",
                    (datetime.now(UTC).isoformat(), task_id),
                )
            return dict(
                await (
                    await db.execute("SELECT * FROM peer_tasks WHERE id=?", (task_id,))
                ).fetchone()
            )
