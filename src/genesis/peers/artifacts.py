"""Owned full results and authorized publication snapshots; no runtime activation."""

import asyncio
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from genesis.peers.disclosure import result_authorized
from genesis.peers.lifecycle_state import current, effects_known, utcnow
from genesis.peers.provider_state import retire_park
from genesis.peers.resources import resource_id
from genesis.peers.tasks import PeerTasks, TaskRefusal
from genesis.security.output_scanner import scan_outbound


def _private(info, mode):
    kind = stat.S_ISDIR if mode == 0o700 else stat.S_ISREG
    if not kind(info.st_mode) or stat.S_IMODE(info.st_mode) != mode or info.st_uid != os.getuid():
        raise ValueError("Peer result unavailable")


def read_private_result(directory, segment):
    """Anchor each directory lookup; scan all bytes, without an artificial size cap."""
    identifier = resource_id(segment["id"])
    session_id = str(uuid.UUID(segment["session_id"]))
    if session_id != segment["session_id"] or str(directory / identifier) != segment["working_dir"]:
        raise ValueError("Peer result unavailable")
    with ExitStack() as stack:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        _private(os.fstat(fd), 0o700)
        for name in (identifier, ".peer-results"):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, fd)
            _private(os.fstat(fd), 0o700)
        fd = os.open(
            f"bg-session-{session_id}.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        source = stack.enter_context(os.fdopen(fd, "rb"))
        _private(os.fstat(source.fileno()), 0o600)
        content = source.read()
    text = content.decode("utf-8")
    if not scan_outbound(text).safe or segment["working_dir"] in text:
        raise ValueError("Peer result unavailable")
    return content


async def _segment(db, task, segment_id=None, session_id=None):
    row = await (
        await db.execute(
            "SELECT s.* FROM peer_segments s JOIN cc_sessions c ON c.id=s.session_id "
            "WHERE s.task_id=? AND s.generation=? AND s.status='drained' "
            "AND c.source_tag='peer_api' AND s.completed_at IS NOT NULL "
            "AND s.completed_at<=s.deadline_at AND s.execution_elapsed_s>=0 "
            "AND s.execution_elapsed_s<=s.reserved_s",
            (task["id"], task["generation"]),
        )
    ).fetchone()
    if (
        row is None
        or (segment_id is not None and row["id"] != segment_id)
        or (session_id is not None and row["session_id"] != session_id)
        or task["hold_reason"] is not None
        or task["work_elapsed_s"] > task["work_limit_s"]
        or await (
            await db.execute(
                "SELECT 1 FROM peer_segments WHERE task_id=? AND status!='drained'", (task["id"],)
            )
        ).fetchone()
    ):
        raise TaskRefusal("result_not_ready", 409)
    return dict(row)


def _tools(value, allowed=None):
    if (
        not isinstance(value, dict)
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"mcp__genesis_peer__[a-z][a-z0-9_]{0,63}", name)
            or (allowed is not None and name not in allowed)
            or type(count) is not int
            or not 0 <= count <= 100
            for name, count in value.items()
        )
        or sum(value.values()) > 100
        or not scan_outbound(json.dumps(value, sort_keys=True)).safe
    ):
        raise TaskRefusal("result_not_ready", 409)
    return value


def _unexpired(task):
    # SQLite serialization fences writers, not time passing during proof awaits.
    if datetime.fromisoformat(task["expires_at"]) <= utcnow():
        raise TaskRefusal("result_not_ready", 409)


class PeerArtifacts:
    def __init__(self, registry, directory):
        self.registry, self.directory = registry, Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("Private peer storage required")

    async def publish(self, binding, session_id, result):
        if (
            result["success"] is not True
            or result["cleanup_confirmed"] is not True
            or result["cancelled"]
            or result["expired"]
        ):
            raise TaskRefusal("result_not_ready", 409)
        async with self.registry.connection() as db:
            await db.execute("BEGIN")
            task = await current(db, binding.task_id, binding.generation, execution=False)
            segment = await _segment(db, task, binding.segment.segment_id, session_id)
            await result_authorized(db, task)
        expected = Path(segment["working_dir"]) / ".peer-results" / f"bg-session-{session_id}.md"
        if str(expected) != result["artifact_path"]:
            raise TaskRefusal("result_not_ready", 409)
        try:
            content = await asyncio.to_thread(read_private_result, self.directory, segment)
        except (OSError, ValueError, UnicodeError):
            raise TaskRefusal("result_not_ready", 409) from None
        digest = hashlib.sha256(content).hexdigest()
        summary = content[:4096].decode("utf-8", errors="ignore")
        tools = _tools(result["tools_summary"], binding.segment.tools)
        async with self.registry.transaction() as db:
            task = await current(db, binding.task_id, binding.generation, execution=False)
            if not await effects_known(db, binding.task_id):
                return  # Commit reconciliation; no success or capacity release.
            await _segment(db, task, binding.segment.segment_id, session_id)
            await result_authorized(db, task)
            prior = await (
                await db.execute("SELECT * FROM peer_artifacts WHERE task_id=?", (binding.task_id,))
            ).fetchone()
            if prior is not None:
                if (
                    prior["segment_id"] != segment["id"]
                    or prior["sha256"] != digest
                    or prior["size_bytes"] != len(content)
                    or task["state"] != "completed"
                ):
                    raise TaskRefusal("state_conflict", 409)
                _unexpired(task)
                return
            if task["state"] != "working":
                raise TaskRefusal("state_conflict", 409)
            stamp = utcnow().isoformat()
            await db.execute(
                "INSERT INTO peer_artifacts(id,task_id,segment_id,sha256,size_bytes,summary,tools_summary,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    binding.task_id,
                    segment["id"],
                    digest,
                    len(content),
                    summary,
                    json.dumps(tools, sort_keys=True),
                    stamp,
                ),
            )
            await retire_park(db, task, completed=True)
            await db.execute(
                "UPDATE peer_tasks SET state='completed',slot_reserved=0,updated_at=? WHERE id=?",
                (stamp, binding.task_id),
            )
            await db.execute(
                "UPDATE peer_task_runtime SET safe_error=NULL WHERE task_id=?", (binding.task_id,)
            )
            _unexpired(task)  # Expired publication rolls back artifact/task/park together.

    async def _published(self, db, identity, task_id, artifact_id=None):
        task = await current(db, task_id, execution=False)
        if task["peer_id"] != identity["peer_id"] or task["epoch"] != identity["epoch"]:
            raise TaskRefusal("not_found", 404)
        artifact = await (
            await db.execute("SELECT * FROM peer_artifacts WHERE task_id=?", (task_id,))
        ).fetchone()
        if artifact is not None and artifact_id is not None and artifact["id"] != artifact_id:
            raise TaskRefusal("not_found", 404)
        if task["state"] != "completed" or artifact is None:
            raise TaskRefusal("result_not_ready", 409)
        segment = await _segment(db, task, artifact["segment_id"])
        if not await effects_known(db, task_id, persist=False):
            raise TaskRefusal("result_not_ready", 409)
        await result_authorized(db, task)
        _unexpired(task)
        return dict(artifact), segment

    async def fetch(self, identity, task_id, artifact_id):
        await PeerTasks(self.registry).owned(identity, task_id)
        async with self.registry.connection() as db:
            await db.execute("BEGIN")
            artifact, segment = await self._published(db, identity, task_id, artifact_id)
        try:
            content = await asyncio.to_thread(read_private_result, self.directory, segment)
            if (
                len(content) != artifact["size_bytes"]
                or hashlib.sha256(content).hexdigest() != artifact["sha256"]
            ):
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            raise TaskRefusal("result_not_ready", 409) from None
        # Serialize the final proof with revocation writers. A WAL read snapshot
        # would keep obsolete grants across awaited authorization queries.
        async with self.registry.transaction() as db:
            final, current_segment = await self._published(db, identity, task_id, artifact_id)
            if final != artifact or current_segment != segment:
                raise TaskRefusal("result_not_ready", 409)
        return content

    async def project(self, identity, rows):
        """Authorize every snapshot in one final batch, with no file awaits afterwards."""
        settings = await self.registry.settings()
        base = settings.get("service_url")
        if not base:
            raise TaskRefusal("not_ready", 503)
        projected = []
        async with self.registry.transaction() as db:
            for original in rows:
                row = await PeerTasks(self.registry).owned(identity, original["id"], db=db)
                runtime = await (
                    await db.execute(
                        "SELECT hold_reason FROM peer_task_runtime WHERE task_id=?", (row["id"],)
                    )
                ).fetchone()
                if runtime is not None and runtime["hold_reason"] in {"approval", "reconciliation"}:
                    row["_peer_reason"] = (
                        "Waiting for individual owner approval"
                        if runtime["hold_reason"] == "approval"
                        else "Operation outcome requires owner reconciliation"
                    )
                    row["state"] = (
                        "input_required"  # Wire projection only; stored hold remains distinct.
                    )
                elif row["state"] in {"failed", "canceled", "rejected"}:
                    row["_peer_reason"] = "Peer task " + row["state"]
                try:
                    artifact, _ = await self._published(db, identity, row["id"])
                    tools = _tools(json.loads(artifact["tools_summary"]))
                    if not scan_outbound(artifact["summary"]).safe:
                        raise TaskRefusal("result_not_ready", 409)
                    row["_peer_result"] = {
                        "artifact_id": artifact["id"],
                        "summary": artifact["summary"],
                        "tools_summary": tools,
                        "url": base + "/tasks/" + row["id"] + "/artifacts/" + artifact["id"],
                    }
                except (TaskRefusal, ValueError, TypeError):
                    pass  # Status can remain visible; withheld result fields never fall back.
                projected.append(row)
            # A prior row can expire while later rows await receipt proofs.
            for row in projected:
                if "_peer_result" in row:
                    try:
                        _unexpired(row)
                    except TaskRefusal:
                        row.pop("_peer_result")
        return projected
