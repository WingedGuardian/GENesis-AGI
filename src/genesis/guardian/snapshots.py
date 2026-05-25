"""Snapshot manager — HOST-SIDE. Incus snapshot create/restore/prune.

Supports both dir and BTRFS storage backends. BTRFS is strongly recommended
(instant CoW snapshots vs. slow full copies). The delete-before-create
strategy ensures at most `retention` guardian snapshots exist at a time.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.config import GuardianConfig

logger = logging.getLogger(__name__)


class SnapshotManager:
    """Manage incus snapshots for the Genesis container."""

    def __init__(self, config: GuardianConfig) -> None:
        self._config = config
        self._container = config.container_name
        self._prefix = config.snapshots.prefix
        self._retention = config.snapshots.retention

    async def check_pool_space(self) -> float:
        """Check genesis pool disk usage. Returns usage percentage (0-100).

        Auto-detects the pool mount point via incus device config.
        Returns 100.0 on any error (fail-safe: assume full).
        """
        # Discover pool name for this container
        rc, pool_name, _ = await _run_subprocess(
            "incus", "config", "device", "get", self._container, "root", "pool",
            timeout=10.0,
        )
        if rc != 0 or not pool_name.strip():
            logger.warning("Failed to detect storage pool — assuming full")
            return 100.0

        pool_path = f"/var/lib/incus/storage-pools/{pool_name.strip()}"
        rc, stdout, stderr = await _run_subprocess(
            "df", "--output=pcent", pool_path,
            timeout=10.0,
        )
        if rc != 0:
            logger.warning("Failed to check pool space at %s: %s", pool_path, stderr)
            return 100.0

        try:
            lines = stdout.strip().splitlines()
            if len(lines) < 2:
                return 100.0
            pct_str = lines[-1].strip().rstrip("%")
            return float(pct_str)
        except (ValueError, IndexError):
            logger.warning("Failed to parse pool space output: %s", stdout)
            return 100.0

    async def safe_to_snapshot(self) -> bool:
        """Check if it's safe to take a snapshot (pool usage below threshold)."""
        max_pct = self._config.snapshots.max_pool_usage_pct
        usage = await self.check_pool_space()
        if usage > max_pct:
            logger.error(
                "Pool usage %.0f%% exceeds %.0f%% threshold — refusing snapshot",
                usage, max_pct,
            )
            return False
        return True

    async def take(self, label: str = "") -> str | None:
        """Create a snapshot. Returns the snapshot name or None on failure.

        Checks disk space before proceeding. Deletes excess snapshots
        before creating the new one to stay within retention limit.
        """
        if not await self.safe_to_snapshot():
            return None

        # Delete-before-create: remove excess snapshots to stay within retention
        existing = await self.list_snapshots()
        if existing and len(existing) >= self._retention:
            for old_name in existing[self._retention - 1:]:
                rc, _, stderr = await _run_subprocess(
                    "incus", "snapshot", "delete", self._container, old_name,
                    timeout=60.0,
                )
                if rc == 0:
                    logger.info("Deleted snapshot before create: %s", old_name)
                else:
                    logger.warning("Failed to delete snapshot %s: %s", old_name, stderr)

        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        name = f"{self._prefix}{ts}"
        if label:
            name = f"{name}-{label}"

        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "create", self._container, name,
            timeout=120.0,  # BTRFS is instant, dir is slow — compromise
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
