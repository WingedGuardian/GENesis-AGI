"""Snapshot manager — incus snapshot create/restore/prune.

NOTE: `dir` storage backend — snapshots are full directory copies (slow,
minutes not seconds). Do NOT take pre-every-change snapshots. Instead:
- Take "healthy" snapshot on schedule (daily or after confirmed recovery)
- Use git revert as primary rollback for code changes
- Reserve snapshot rollback for catastrophic failures only
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from genesis.guardian.config import GuardianConfig
from genesis.guardian.health_signals import _run_subprocess

logger = logging.getLogger(__name__)


class SnapshotManager:
    """Manage incus snapshots for the Genesis container."""

    def __init__(self, config: GuardianConfig) -> None:
        self._config = config
        self._container = config.container_name
        self._prefix = config.snapshots.prefix
        self._retention = config.snapshots.retention

    async def take(self, label: str = "") -> str | None:
        """Create a snapshot. Returns the snapshot name or None on failure."""
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        name = f"{self._prefix}{ts}"
        if label:
            name = f"{name}-{label}"

        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "create", self._container, name,
            timeout=300.0,  # dir backend can be very slow
        )
        if rc != 0:
            logger.error("Failed to create snapshot %s: %s", name, stderr)
            return None

        logger.info("Created snapshot: %s", name)
        return name

    async def restore(self, name: str) -> bool:
        """Restore a snapshot. Returns True on success."""
        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "restore", self._container, name,
            timeout=300.0,
        )
        if rc != 0:
            logger.error("Failed to restore snapshot %s: %s", name, stderr)
            return False

        logger.info("Restored snapshot: %s", name)
        return True

    async def list_snapshots(self) -> list[str]:
        """List all guardian snapshots, newest first."""
        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "list", self._container, "--format", "json",
            timeout=30.0,
        )
        if rc != 0:
            logger.warning("Failed to list snapshots: %s", stderr)
            return []

        try:
            snapshots = json.loads(stdout)
            names = [
                s.get("name", "")
                for s in snapshots
                if isinstance(s, dict)
                and s.get("name", "").startswith(self._prefix)
            ]
            # Sort by name (contains timestamp) — newest first
            names.sort(reverse=True)
            return names
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse snapshot list: %s", exc)
            return []

    async def prune(self) -> int:
        """Delete oldest snapshots past retention limit. Returns count deleted."""
        snapshots = await self.list_snapshots()
        if len(snapshots) <= self._retention:
            return 0

        # Always keep the most recent "healthy" snapshot
        healthy = [s for s in snapshots if s.endswith("-healthy")]
        to_keep = set(snapshots[:self._retention])
        if healthy:
            to_keep.add(healthy[0])  # most recent healthy

        to_delete = [s for s in snapshots if s not in to_keep]
        deleted = 0
        for name in to_delete:
            rc, _, stderr = await _run_subprocess(
                "incus", "snapshot", "delete", self._container, name,
                timeout=120.0,
            )
            if rc == 0:
                logger.info("Pruned snapshot: %s", name)
                deleted += 1
            else:
                logger.warning("Failed to prune snapshot %s: %s", name, stderr)

        return deleted

    async def mark_healthy(self) -> str | None:
        """Take a snapshot labeled 'healthy'."""
        return await self.take(label="healthy")

    async def get_latest_healthy(self) -> str | None:
        """Get the name of the most recent 'healthy' snapshot."""
        snapshots = await self.list_snapshots()
        for name in snapshots:
            if name.endswith("-healthy"):
                return name
        return None
