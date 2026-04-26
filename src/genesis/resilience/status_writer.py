"""StatusFileWriter — writes system status to ~/.genesis/status.json."""

from __future__ import annotations

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

    def set_extra_data(self, key: str, value) -> None:
        """Merge additional data into the next status write.

        Used by the bridge to inject adapter/polling health.
        """
        self._extra_data[key] = value

    async def write(self) -> None:
        """Write current system status to JSON file."""
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
            },
            "queue_depths": {
                "deferred_work": deferred_count,
                "dead_letter": dead_letter_count,
                "pending_embeddings": embedding_count,
            },
            "last_recovery": None,
            "human_summary": summary,
        }
        if failing_jobs:
            data["failing_jobs"] = failing_jobs

        # Pause state (if runtime available)
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            data["paused_state"] = {
                "is_paused": rt.paused,
                "reason": rt.pause_reason,
                "since": rt.paused_since.isoformat() if rt.paused_since else None,
            }
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

        # Merge bridge/adapter health if provided
        if self._extra_data:
            data.update(self._extra_data)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Atomic write: temp file + os.replace to prevent partial reads
            # by concurrent MCP server processes reading status.json.
            import os
            import tempfile

            content = json.dumps(data, indent=2)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), suffix=".tmp",
            )
            fd_closed = False
            try:
                os.write(fd, content.encode())
                os.close(fd)
                fd_closed = True
                os.replace(tmp_path, str(self._path))
            except Exception:
                if not fd_closed:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
        except Exception:
            logger.error(
                "Status file write FAILED at %s — external monitoring will see stale data",
                self._path, exc_info=True,
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
            name for name, entry in job_health.items()
            if entry.get("consecutive_failures", 0) >= 2
        ]

    def _build_summary(
        self, state, total_queued: int, failing_jobs: list[str] | None = None,
    ) -> str:
        """Describe the worst conditions concisely."""
        parts = []

        from genesis.resilience.state import (
            CCStatus,
            CloudStatus,
            EmbeddingStatus,
            MemoryStatus,
        )

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
