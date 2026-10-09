"""Peer-only provider lineage on the existing park store and private transaction."""

from __future__ import annotations

import hashlib
import json

from genesis.cc import rate_limit_park as scheduling
from genesis.cc import rate_limit_resume_config as config
from genesis.cc.rate_limit_reset import parse_reset
from genesis.db.crud import cc_rate_limit_parks as parks
from genesis.db.crud import direct_session_queue as queue
from genesis.peers.lifecycle_state import (
    bound,
    current,
    disclosure_authorized,
    effects_known,
    utcnow,
)
from genesis.peers.tasks import TaskRefusal


def peer_park(park):
    """Every legacy reader must exclude either explicit peer lineage marker."""
    try:
        payload = json.loads(park["payload_json"])
    except (ValueError, TypeError, RecursionError):
        return False  # Legacy backoff and owner alerts own corrupt-payload recovery.
    return isinstance(payload, dict) and (
        payload.get("source_tag") == "peer_api" or "peer_task_id" in payload
    )


def lineage(park, task):
    payload = json.loads(park["payload_json"])
    if (
        not isinstance(payload, dict)
        or payload.keys() != {"source_tag", "peer_task_id", "epoch", "generation", "segment_id"}
        or payload["source_tag"] != "peer_api"
        or payload["peer_task_id"] != task["id"]
        or payload["epoch"] != task["epoch"]
        or type(payload["generation"]) is not int
        or payload["generation"] < 0
        or park["kind"] != "direct_session"
    ):
        raise TaskRefusal("state_conflict", 409)
    return payload


async def retire_park(db, task, *, expired=False, completed=False):
    runtime = await (
        await db.execute("SELECT park_id FROM peer_task_runtime WHERE task_id=?", (task["id"],))
    ).fetchone()
    if runtime is None or runtime["park_id"] is None:
        return
    park = await parks.get_by_id(db, runtime["park_id"])
    if park is None:
        raise TaskRefusal("state_conflict", 409)
    lineage(park, task)
    if park["status"] in {"resumed", "cancelled", "expired"}:
        return
    changed = await parks.mark_terminal_if_unchanged(
        db,
        park["id"],
        "resumed" if completed else "expired" if expired else "cancelled",
        expected_status=park["status"],
        expected_claimed_at=park["claimed_at"],
        expected_updated_at=park["updated_at"],
        commit=False,
    )
    if not changed:
        raise TaskRefusal("state_conflict", 409)


class PeerProviderState:
    def __init__(self, registry):
        self.registry = registry

    async def park(self, binding, exc):
        if config.effective_mode() == "off":
            return False
        now, cfg = utcnow(), config.load_config()
        try:
            kind, reset = parse_reset(
                raw_event=getattr(exc, "raw_event", None),
                raw_text=getattr(exc, "raw_text", None),
                now=now,
            )
        except Exception:
            kind, reset = "unknown", None
        async with self.registry.transaction() as db:
            task = await bound(db, binding)
            generation = task["generation"] + 1
            payload = {
                "source_tag": "peer_api",
                "peer_task_id": task["id"],
                "epoch": task["epoch"],
                "generation": generation,
                "segment_id": binding.segment.segment_id,
            }
            if task["park_id"]:
                prior = await parks.get_by_id(db, task["park_id"])
                if prior is None:
                    raise TaskRefusal("state_conflict", 409)
                lineage(prior, task)
                status = await parks.relimit(
                    db,
                    prior["id"],
                    reset_at=reset.isoformat() if reset else None,
                    next_attempt_at=max(
                        scheduling.next_attempt_at(reset, now, cfg),
                        scheduling.backoff_next_attempt(prior["attempts"] + 1, now, cfg),
                    ),
                    needs_user_at_attempts=config.knob_int(cfg, "needs_user_attempts"),
                    commit=False,
                )
                if not status:
                    raise TaskRefusal("state_conflict", 409)
                park_id = prior["id"]
                await db.execute(
                    "UPDATE cc_rate_limit_parks SET payload_json=?,limit_kind=?,raw_signal=NULL WHERE id=?",
                    (json.dumps(payload), kind, park_id),
                )
            else:
                park_id = await parks.upsert_open_park(
                    db,
                    kind="direct_session",
                    dedup_key=hashlib.sha256(("peer_api:" + task["id"]).encode()).hexdigest(),
                    payload=payload,
                    origin_session_id=None,
                    limit_kind=kind,
                    raw_signal=None,
                    reset_at=reset.isoformat() if reset else None,
                    next_attempt_at=scheduling.next_attempt_at(reset, now, cfg),
                    commit=False,
                )
            await db.execute(
                "UPDATE peer_tasks SET generation=?,updated_at=? WHERE id=?",
                (generation, now.isoformat(), task["id"]),
            )
            await db.execute(
                "UPDATE peer_task_runtime SET hold_reason='provider',hold_segment_id=?,"
                "hold_capability=NULL,hold_digest=NULL,approval_id=NULL,park_id=? WHERE task_id=?",
                (binding.segment.segment_id, park_id, task["id"]),
            )
            return True

    async def resume(self, park_id, *, now=None):
        if config.effective_mode() != "live":
            return False
        now = now or utcnow()
        async with self.registry.transaction() as db:
            park = await parks.get_by_id(db, park_id)
            if park is None or not peer_park(park):
                return False
            payload = json.loads(park["payload_json"])
            task = await current(db, payload["peer_task_id"])
            payload = lineage(park, task)
            if (
                park["status"] != "parked"
                or (park["next_attempt_at"] and park["next_attempt_at"] > now.isoformat())
                or task["hold_reason"] != "provider"
                or task["park_id"] != park_id
                or payload["generation"] != task["generation"]
                or payload["segment_id"] != task["hold_segment_id"]
            ):
                return False
            await disclosure_authorized(db, task)
            drained = await (
                await db.execute(
                    "SELECT 1 FROM peer_segments WHERE id=? AND task_id=? AND status='drained'",
                    (task["hold_segment_id"], task["id"]),
                )
            ).fetchone()
            if drained is None or not await effects_known(db, task["id"]):
                return False
            counts = await (
                await db.execute(
                    "SELECT COALESCE(SUM(slot_reserved),0),COALESCE(SUM(CASE WHEN peer_id=? "
                    "THEN slot_reserved ELSE 0 END),0) FROM peer_tasks",
                    (task["peer_id"],),
                )
            ).fetchone()
            if counts[0] >= 2 or counts[1] >= 2:
                return False
            if not await parks.claim(db, park_id, commit=False):
                return False
            item = queue.prepare({"source_tag": "peer_api", "peer_task_id": task["id"]})
            await queue.insert_prepared(db, item)
            await db.execute(
                "UPDATE peer_tasks SET generation=generation+1,state='submitted',slot_reserved=1,"
                "queue_id=?,updated_at=? WHERE id=?",
                (item.id, now.isoformat(), task["id"]),
            )
            await db.execute(
                "UPDATE peer_task_runtime SET hold_reason=NULL WHERE task_id=?", (task["id"],)
            )
            return True
