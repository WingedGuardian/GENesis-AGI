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

    async def _get_pool_free_bytes(self) -> tuple[int, int] | None:
        """Get (total_bytes, free_bytes) for the storage pool. None on failure."""
        rc, pool_name, _ = await _run_subprocess(
            "incus", "config", "device", "get", self._container, "root", "pool",
            timeout=10.0,
        )
        if rc != 0 or not pool_name.strip():
            return None

        pool_path = f"/var/lib/incus/storage-pools/{pool_name.strip()}"
        rc, stdout, stderr = await _run_subprocess(
            "df", "--output=size,avail", "--block-size=1", pool_path,
            timeout=10.0,
        )
        if rc != 0:
            logger.warning("Failed to get pool bytes at %s: %s", pool_path, stderr)
            return None

        try:
            lines = stdout.strip().splitlines()
            if len(lines) < 2:
                return None
            parts = lines[-1].split()
            if len(parts) < 2:
                return None
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return None

    async def safe_to_snapshot(
        self, snapshot_size_history: list[int] | None = None,
    ) -> bool:
        """Check if it's safe to take a snapshot using headroom-based gating.

        Strategy:
        - With snapshot size history: require free > max(min_headroom_gb, 2x avg
          of last 3 snapshot sizes). Adapts to actual snapshot sizes.
        - Without history: require at least 10% of pool free (safe default for
          first snapshots before any size data is available).
        - If pool detection fails entirely: fall back to percentage threshold
          (max_pool_usage_pct) for robustness.
        """
        pool_info = await self._get_pool_free_bytes()
        if pool_info is None:
            # Can't get byte-level info — fall back to percentage check
            max_pct = self._config.snapshots.max_pool_usage_pct
            usage = await self.check_pool_space()
            if usage > max_pct:
                logger.error(
                    "Pool usage %.0f%% exceeds %.0f%% threshold — refusing snapshot",
                    usage, max_pct,
                )
                return False
            return True

        total_bytes, free_bytes = pool_info
        min_headroom = int(self._config.snapshots.min_headroom_gb * 1024**3)

        history = snapshot_size_history or []
        if not history:
            # No history — require at least 10% of pool free
            threshold = int(total_bytes * 0.10)
            if free_bytes < threshold:
                logger.error(
                    "Pool free %d bytes < 10%% threshold %d bytes — refusing snapshot",
                    free_bytes, threshold,
                )
                return False
            return True

        # History available — require free > max(min_headroom, 2x avg last 3)
        recent = history[-3:]
        avg_size = sum(recent) // len(recent)
        required = max(min_headroom, 2 * avg_size)

        if free_bytes < required:
            logger.error(
                "Pool free %d bytes < required headroom %d bytes "
                "(min_headroom=%d, 2x_avg=%d, history=%d samples) — refusing snapshot",
                free_bytes, required, min_headroom, 2 * avg_size, len(recent),
            )
            return False

        logger.info(
            "Headroom check passed: %d bytes free, %d required "
            "(avg snapshot %d bytes, %d samples)",
            free_bytes, required, avg_size, len(recent),
        )
        return True

    async def take(
        self,
        label: str = "",
        snapshot_size_history: list[int] | None = None,
    ) -> str | None:
        """Create a snapshot. Returns the snapshot name or None on failure.

        Checks disk space before proceeding. Deletes excess snapshots
        before creating the new one to stay within retention limit.
        """
        if not await self.safe_to_snapshot(snapshot_size_history):
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

        # Measure pool free before snapshot (for size estimation after)
        pre_info = await self._get_pool_free_bytes()

        rc, stdout, stderr = await _run_subprocess(
            "incus", "snapshot", "create", self._container, name,
            timeout=120.0,  # BTRFS is instant, dir is slow — compromise
        )
        if rc != 0:
            logger.error("Failed to create snapshot %s: %s", name, stderr)
            return None

        logger.info("Created snapshot: %s", name)

        # Record snapshot size estimate (delta in pool free space)
        if snapshot_size_history is not None and pre_info is not None:
            post_info = await self._get_pool_free_bytes()
            if post_info is not None:
                _, pre_free = pre_info
                _, post_free = post_info
                size_estimate = max(0, pre_free - post_free)
                if size_estimate > 0:
                    snapshot_size_history.append(size_estimate)
                    # Keep last 5 entries
                    del snapshot_size_history[:-5]
                    logger.info(
                        "Snapshot size estimate: %d bytes (%d samples in history)",
                        size_estimate, len(snapshot_size_history),
                    )

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

    async def mark_healthy(
        self, snapshot_size_history: list[int] | None = None,
    ) -> str | None:
        """Take a snapshot labeled 'healthy'."""
        return await self.take(
            label="healthy", snapshot_size_history=snapshot_size_history,
        )

    async def get_latest_healthy(self) -> str | None:
        """Get the name of the most recent 'healthy' snapshot."""
        snapshots = await self.list_snapshots()
        for name in snapshots:
            if name.endswith("-healthy"):
                return name
        return None
