"""StatusFileWriter — writes system status to ~/.genesis/status.json."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class StatusFileWriter:
    def __init__(
        self,
        *,
        state_machine,
        deferred_queue=None,
        dead_letter=None,
        pending_embeddings_db=None,
        runtime=None,
        path: str = "~/.genesis/status.json",
    ) -> None:
        self._state_machine = state_machine
        self._deferred_queue = deferred_queue
        self._dead_letter = dead_letter
        self._pending_embeddings_db = pending_embeddings_db
        self._runtime = runtime
        self._path = Path(path).expanduser()
        self._extra_data: dict = {}
        self._write_gate = asyncio.Lock()
        self._publication: asyncio.Future | None = None
        self._closed = False

    def set_extra_data(self, key: str, value) -> None:
        """Merge additional data into the next status write.

        Used by the bridge to inject adapter/polling health.
        """
        self._extra_data[key] = value

    async def write(self) -> None:
        """Collect on-loop, then await ordered atomic publication off-loop."""
        if self._closed:
            raise RuntimeError("StatusFileWriter is closed")
        await self._write_gate.acquire()
        submitted = False
        try:
            if self._closed:
                raise RuntimeError("StatusFileWriter is closed")
            data = await self._snapshot()
            try:
                payload = json.dumps(data, indent=2).encode()
            except Exception:
                logger.error("Status file serialization FAILED at %s", self._path, exc_info=True)
                return
            future = asyncio.get_running_loop().run_in_executor(
                None, self._publish, self._path, payload,
            )
            self._publication = future
            future.add_done_callback(self._publication_finished)
            submitted = True
            # Cancellation ends this await, not the actual filesystem work.
            # Only its completion callback may admit the next snapshot/write.
            await asyncio.shield(future)
        finally:
            if not submitted:
                self._write_gate.release()

    def _publication_finished(self, future: asyncio.Future) -> None:
        try:
            exc = future.exception()
            if exc is not None:
                logger.error(
                    "Status publication worker FAILED at %s", self._path,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
        finally:
            self._publication = None
            self._write_gate.release()

    async def aclose(self) -> None:
        """Reject new writes and drain publication, even after a canceled close.

        An already-admitted snapshot may finish. Waiting writers recheck closed
        before collecting. Do not shield acquire: an abandoned acquisition could
        otherwise keep this gate locked after its caller has gone away.
        """
        self._closed = True
        async with self._write_gate:
            pass

    async def _snapshot(self) -> dict:
        """Read runtime and async database state on their owning event loop."""
        from datetime import UTC, datetime

        state = self._state_machine.current

        # Queue depths
        deferred_count = 0
        if self._deferred_queue is not None:
            deferred_count = await self._deferred_queue.count_pending()

        dead_letter_count = 0
        if self._dead_letter is not None:
            dead_letter_count = await self._dead_letter.get_pending_count()

        embedding_count = 0
        if self._pending_embeddings_db is not None:
            from genesis.db.crud import pending_embeddings

            embedding_count = await pending_embeddings.count_pending(self._pending_embeddings_db)

        total_queued = deferred_count + dead_letter_count + embedding_count

        # Collect job health failures
        failing_jobs = self._get_failing_jobs()

        # Build human summary
        summary = self._build_summary(state, total_queued, failing_jobs)

        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "resilience_state": {
                "cloud": state.cloud.name,
                "memory": state.memory.name,
                "embedding": state.embedding.name,
                "cc": state.cc.name,
                # tmp_pressure was historically omitted here despite being a real
                # axis feeding to_legacy_degradation_level — added alongside network.
                "tmp_pressure": state.tmp_pressure.name,
                "network": state.network.name,
            },
            "queue_depths": {
                "deferred_work": deferred_count,
                "dead_letter": dead_letter_count,
                "pending_embeddings": embedding_count,
            },
            "last_recovery": None,
            "human_summary": summary,
        }
        from genesis.observability.agent_api_health import sample

        data["agent_api"] = await sample(self._runtime)
        if failing_jobs:
            data["failing_jobs"] = failing_jobs

        # Pause state and workload info (if runtime available)
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            data["paused_state"] = {
                "is_paused": rt.paused,
                "reason": rt.pause_reason,
                "since": rt.paused_since.isoformat() if rt.paused_since else None,
            }
            # Heavy workload flag — read by watchdog to defer restarts
            data["heavy_workload"] = rt.heavy_workload
            # Uptime since bootstrap — read by watchdog for stabilization
            if rt._bootstrap_completed_at is not None:
                data["uptime_s"] = (datetime.now(UTC) - rt._bootstrap_completed_at).total_seconds()
        except Exception:
            pass

        # Scheduler liveness heartbeats — enables the external watchdog to
        # detect zombie schedulers (process alive, scheduler dead).
        scheduler_heartbeats = {}
        try:
            rt_inst = self._runtime
            if rt_inst is not None:
                jh = rt_inst.job_health
                # Awareness: use the awareness_tick job timestamp
                at_entry = jh.get("awareness_tick", {})
                if at_entry.get("last_run"):
                    scheduler_heartbeats["awareness"] = at_entry["last_run"]
                # Surplus: use surplus_dispatch job timestamp
                sd_entry = jh.get("surplus_dispatch", {})
                if sd_entry.get("last_run"):
                    scheduler_heartbeats["surplus"] = sd_entry["last_run"]
        except Exception:
            pass
        if scheduler_heartbeats:
            data["scheduler_heartbeats"] = scheduler_heartbeats

        # Reflex-arc ingestor counters — lets the standalone health MCP (a
        # separate process reading this file) surface live nerve state. The
        # isinstance guard is load-bearing: a non-dict (absent ingestor, mock
        # runtime in tests) must never reach json.dumps and break the whole
        # status write.
        try:
            ingestor = getattr(self._runtime, "_reflex_ingestor", None)
            stats = getattr(ingestor, "stats", None)
            if isinstance(stats, dict):
                data["reflex"] = stats
        except Exception:
            pass

        # Connectivity snapshot from the network sentinel. Carries the sentinel's
        # OWN last_probe_at so the watchdog can staleness-check the PROBE (not the
        # file): status.json's top-level timestamp refreshes from THIS 60s loop
        # independently, so a dead sentinel + live status-writer would otherwise
        # look fresh forever with a frozen network field. isinstance guard is
        # load-bearing (absent sentinel / mock runtime → skip, never crash write).
        try:
            sentinel = getattr(self._runtime, "_network_sentinel", None)
            snap = sentinel.snapshot() if sentinel is not None else None
            if isinstance(snap, dict):
                data["network"] = snap
        except Exception:
            pass

        # Merge bridge/adapter health if provided
        if self._extra_data:
            data.update(self._extra_data)

        return data

    @staticmethod
    def _publish(path: Path, payload: bytes) -> None:
        """Perform file I/O only; the caller owns ordering and immutable bytes."""
        # Preserve the existing contract: directory-creation errors propagate;
        # failures inside temporary-file publication are logged and nonfatal.
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Atomic write: temp file + os.replace to prevent partial reads
            # by concurrent MCP server processes reading status.json.
            import os
            import tempfile

            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent),
                suffix=".tmp",
            )
            handle = None
            try:
                handle = os.fdopen(fd, "wb")
                with handle:
                    # BufferedWriter writes the whole payload or raises; a raw
                    # os.write may return a successful but incomplete write.
                    handle.write(payload)
                os.replace(tmp_path, str(path))
            except Exception:
                if handle is None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
        except Exception:
            logger.error(
                "Status file write FAILED at %s — external monitoring will see stale data",
                path,
                exc_info=True,
            )

    def _get_failing_jobs(self) -> list[str]:
        """Return names of scheduled jobs with 2+ consecutive failures."""
        if self._runtime is None:
            return []
        try:
            job_health = self._runtime.job_health
        except Exception:
            return []
        return [
            name for name, entry in job_health.items() if entry.get("consecutive_failures", 0) >= 2
        ]

    def _build_summary(
        self,
        state,
        total_queued: int,
        failing_jobs: list[str] | None = None,
    ) -> str:
        """Describe the worst conditions concisely."""
        parts = []

        from genesis.resilience.state import (
            CCStatus,
            CloudStatus,
            EmbeddingStatus,
            MemoryStatus,
            NetworkStatus,
        )

        if state.network != NetworkStatus.NORMAL:
            parts.append(f"Internet {state.network.name.lower()}")
        if state.cloud != CloudStatus.NORMAL:
            parts.append(f"Cloud {state.cloud.name.lower()}")
        if state.memory != MemoryStatus.NORMAL:
            parts.append(f"Memory {state.memory.name.lower()}")
        if state.embedding != EmbeddingStatus.NORMAL:
            parts.append(f"Embedding service {state.embedding.name.lower()}")
        if state.cc != CCStatus.NORMAL:
            parts.append(f"CC sessions {state.cc.name.lower()}")
        if failing_jobs:
            parts.append(f"{len(failing_jobs)} scheduled job(s) failing")

        summary = "All systems normal." if not parts else ", ".join(parts) + "."

        if total_queued > 0:
            summary += f" {total_queued} items queued."

        return summary
