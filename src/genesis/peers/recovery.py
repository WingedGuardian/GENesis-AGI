"""Recover private peer execution without requiring an executable runtime."""

import json
import math
from pathlib import Path

from genesis.cc.peer_segment import PeerSegment
from genesis.db.crud import cc_sessions
from genesis.peers.artifacts import PeerArtifacts
from genesis.peers.lifecycle_state import PeerLifecycleState, current, effects_known, utcnow
from genesis.peers.session import PeerSessionBinding
from genesis.peers.tasks import TERMINAL, TaskRefusal


def recovered_binding(segment, directory):
    """Recovery may stop a named scope, but never execute a reconstructed invocation."""
    work = str(Path(directory) / segment["id"])
    return PeerSessionBinding(
        segment["task_id"],
        PeerSegment(
            segment["id"],
            segment["deadline_at"],
            tuple(
                "mcp__genesis_peer__" + name
                for name in ("task_context", "resources_list", "resource_read")
            ),
        ),
        segment["generation"],
        str(Path(work) / "facade.json"),
        work,
    )


def recovery_elapsed(segment):
    if segment["status"] in {"prepared", "blocked", "drained"} and segment["started_at"] is None:
        return 0, False  # Committed begin is mandatory before any invocation.
    completed, elapsed = segment["completed_at"], segment["execution_elapsed_s"]
    if (
        isinstance(completed, (int, float))
        and math.isfinite(completed)
        and completed <= segment["deadline_at"]
        and isinstance(elapsed, (int, float))
        and math.isfinite(elapsed)
        and 0 <= elapsed <= segment["reserved_s"]
    ):
        return elapsed, False
    return segment["reserved_s"], True


class PeerRecovery:
    def __init__(self, registry, directory, *, research=None, publication_tools=()):
        if publication_tools and research is None:
            raise ValueError("Research publication requires receipt validation")
        self.registry = registry
        self.state = PeerLifecycleState(registry, directory)
        self.artifacts = PeerArtifacts(registry, directory)
        self.artifacts.research = research
        self.publication_tools = tuple(publication_tools)

    async def run(self):
        """Union task disposition with segment drain; neither implies the other."""
        async with self.registry.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT s.* FROM peer_segments s JOIN peer_tasks t ON t.id=s.task_id "
                    "WHERE s.status!='drained' OR t.state IN ('submitted','working','input_required') "
                    "ORDER BY s.task_id,s.generation"
                )
            ).fetchall()
        for row in rows:
            segment = dict(row)
            binding = recovered_binding(segment, self.state.directory)
            clean = segment["status"] == "drained"
            if not clean and segment["working_dir"] == binding.working_dir:
                try:
                    await binding.segment.stop_and_drain()
                    clean = True
                except Exception:
                    clean = False
            async with self.registry.transaction() as db:
                await db.execute(
                    "UPDATE peer_operations SET status='unknown',updated_at=? "
                    "WHERE segment_id=? AND status='executing'",
                    (utcnow().isoformat(), segment["id"]),
                )
                await effects_known(db, segment["task_id"])
            elapsed, uncertain = recovery_elapsed(segment)
            await self.state.settle(binding, elapsed, clean=clean, uncertain=uncertain)
            await self._orphan_sessions(segment)
        async with self.registry.connection() as db:
            tasks = await (
                await db.execute(
                    "SELECT * FROM peer_tasks WHERE state IN ('submitted','working','input_required')"
                )
            ).fetchall()
        for row in tasks:
            await self._disposition(dict(row))
        async with self.registry.connection() as db:
            blocked = await (
                await db.execute("SELECT 1 FROM peer_segments WHERE status!='drained' LIMIT 1")
            ).fetchone()
        return blocked is None

    async def _orphan_sessions(self, segment):
        """Terminalize exact active peer lineage; never alter unrelated sessions."""
        async with self.registry.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM cc_sessions WHERE source_tag='peer_api' AND status='active'"
                )
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata"])
                    matches = (
                        metadata["peer_task_id"] == segment["task_id"]
                        and metadata["peer_segment_id"] == segment["id"]
                        and type(metadata["peer_generation"]) is int
                        and metadata["peer_generation"] == segment["generation"]
                        and metadata["caller_context"] == "peer_api:" + segment["task_id"]
                    )
                except (KeyError, TypeError, ValueError):
                    matches = False
                if matches:
                    await cc_sessions.update_status(db, row["id"], status="failed")

    async def _disposition(self, task):
        async with self.registry.transaction() as db:
            fresh = await (
                await db.execute("SELECT * FROM peer_tasks WHERE id=?", (task["id"],))
            ).fetchone()
            if fresh is None or fresh["state"] in TERMINAL:
                return
            if not await effects_known(db, task["id"]):
                return
            segment = await (
                await db.execute(
                    "SELECT * FROM peer_segments WHERE task_id=? AND generation=?",
                    (fresh["id"], fresh["generation"]),
                )
            ).fetchone()
            try:
                authorized = await current(db, task["id"], execution=False)
            except TaskRefusal:
                authorized = None
        if authorized is None:
            await self.state.end(
                task["id"],
                "canceled" if fresh["cancel_requested"] else "failed",
                "Peer task permission unavailable",
                generation=fresh["generation"],
            )
            return
        if authorized["hold_reason"] is not None:
            if (
                authorized["hold_reason"] != "reconciliation"
                and authorized["work_elapsed_s"] >= authorized["work_limit_s"]
            ):
                await self.state.end(
                    task["id"],
                    "failed",
                    "Peer work allowance exhausted",
                    generation=fresh["generation"],
                )
            return
        if fresh["state"] == "submitted" and segment is None:
            return  # Accepted/consumed continuation queue survives unchanged.
        if segment is not None and segment["status"] == "drained":
            result = await self._recorded_result(dict(segment))
            if result is not None:
                try:
                    binding = recovered_binding(segment, self.state.directory)
                    await self.artifacts.publish(
                        binding,
                        segment["session_id"],
                        result,
                        allowed_tools=binding.segment.tools + self.publication_tools,
                    )
                    return
                except (TaskRefusal, ValueError, OSError):
                    pass
        await self.state.end(
            task["id"], "failed", "Peer session interrupted", generation=fresh["generation"]
        )

    async def _recorded_result(self, segment):
        async with self.registry.connection() as db:
            row = await cc_sessions.get_by_id(db, segment["session_id"])
        try:
            metadata = json.loads(row["metadata"])
            if (
                row["status"] != "completed"
                or row["source_tag"] != "peer_api"
                or metadata["peer_task_id"] != segment["task_id"]
                or metadata["peer_segment_id"] != segment["id"]
                or type(metadata["peer_generation"]) is not int
                or metadata["peer_generation"] != segment["generation"]
                or metadata["caller_context"] != "peer_api:" + segment["task_id"]
                or metadata["cleanup_confirmed"] is not True
                or metadata["peer_cancelled"] is not False
                or metadata["peer_expired"] is not False
                or metadata["error"] is not None
            ):
                return None
            return dict(
                success=True,
                cleanup_confirmed=True,
                cancelled=False,
                expired=False,
                artifact_path=metadata["result_artifact_path"],
                tools_summary=metadata["tools_summary"],
            )
        except (KeyError, TypeError, ValueError):
            return None
