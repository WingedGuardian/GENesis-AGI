"""Tests for Guardian snapshot manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from genesis.guardian.config import GuardianConfig
from genesis.guardian.snapshots import SnapshotManager


@pytest.fixture
def config() -> GuardianConfig:
    return GuardianConfig()


@pytest.fixture
def manager(config: GuardianConfig) -> SnapshotManager:
    return SnapshotManager(config)


def _mock_subprocess(rc: int = 0, stdout: str = "", stderr: str = ""):
    """Simple mock that returns the same result for all subprocess calls."""
    async def mock(*args, **kwargs):
        return (rc, stdout, stderr)
    return mock


def _mock_subprocess_smart(
    snapshot_rc: int = 0,
    snapshot_stdout: str = "",
    pool_usage_pct: int = 20,
):
    """Mock that handles pool detection, df, and snapshot operations."""
    async def mock(*args, **kwargs):
        cmd = args[0] if args else ""
        if cmd == "incus" and len(args) > 3 and args[1] == "config":
            # Pool detection: incus config device get ...
            return (0, "genesis-pool\n", "")
        if cmd == "df":
            # Disk space check
            return (0, f"Use%\n {pool_usage_pct}%\n", "")
        if cmd == "incus" and len(args) > 2 and args[1] == "snapshot":
            return (snapshot_rc, snapshot_stdout, "")
        return (snapshot_rc, snapshot_stdout, "")
    return mock


class TestSnapshotTake:

    @pytest.mark.asyncio
    async def test_take_success(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_smart(snapshot_rc=0),
        ):
            name = await manager.take(label="test")
        assert name is not None
        assert name.startswith("guardian-")
        assert name.endswith("-test")

    @pytest.mark.asyncio
    async def test_take_failure(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_smart(snapshot_rc=1),
        ):
            name = await manager.take()
        assert name is None

    @pytest.mark.asyncio
    async def test_take_no_label(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_smart(snapshot_rc=0),
        ):
            name = await manager.take()
        assert name is not None
        assert "-test" not in name

    @pytest.mark.asyncio
    async def test_take_refuses_when_pool_full(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_smart(pool_usage_pct=95),
        ):
            name = await manager.take()
        assert name is None


class TestSnapshotRestore:

    @pytest.mark.asyncio
    async def test_restore_success(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(0, ""),
        ):
            ok = await manager.restore("guardian-20260325-120000-healthy")
        assert ok is True

    @pytest.mark.asyncio
    async def test_restore_failure(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(1, "", "not found"),
        ):
            ok = await manager.restore("nonexistent")
        assert ok is False


class TestSnapshotList:

    @pytest.mark.asyncio
    async def test_list_snapshots(self, manager: SnapshotManager) -> None:
        snapshots = [
            {"name": "guardian-20260325-120000"},
            {"name": "guardian-20260325-100000-healthy"},
            {"name": "other-snapshot"},  # should be filtered out
        ]
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(0, json.dumps(snapshots)),
        ):
            names = await manager.list_snapshots()
        assert len(names) == 2
        assert "other-snapshot" not in names
        # Newest first
        assert names[0] == "guardian-20260325-120000"

    @pytest.mark.asyncio
    async def test_list_empty(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(0, "[]"),
        ):
            names = await manager.list_snapshots()
        assert names == []


def _meta(names_ages: list[tuple[str, float]]) -> list[tuple[str, datetime]]:
    """Build (name, created_at) tuples from (name, age_in_days), newest first."""
    now = datetime.now(UTC)
    return [(name, now - timedelta(days=age)) for name, age in names_ages]


class TestSnapshotPrune:

    @pytest.mark.asyncio
    async def test_prune_within_retention(self, manager: SnapshotManager) -> None:
        """A single fresh snapshot (== retention=1) is kept, nothing pruned."""
        with patch.object(
            manager, "_list_snapshots_with_meta",
            return_value=_meta([("guardian-1", 0.0)]),
        ):
            deleted = await manager.prune()
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_prune_over_retention(self, manager: SnapshotManager) -> None:
        """Should prune oldest snapshots past retention (1)."""
        snapshots = _meta([(f"guardian-{i}", float(3 - i)) for i in range(3, 0, -1)])
        with (
            patch.object(manager, "_list_snapshots_with_meta", return_value=snapshots),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _mock_subprocess(0, ""),
            ),
        ):
            deleted = await manager.prune()
        assert deleted == 2  # 3 - 1 = 2

    @pytest.mark.asyncio
    async def test_prune_preserves_healthy(self, manager: SnapshotManager) -> None:
        """The most recent healthy snapshot should be preserved even if old."""
        snapshots = _meta([
            ("guardian-6", 1.0), ("guardian-5", 2.0), ("guardian-4", 3.0),
            ("guardian-3", 4.0), ("guardian-2", 5.0),
            ("guardian-1-healthy", 6.0),  # oldest but healthy
        ])
        deleted_names: list[str] = []

        async def track_delete(*args, **kwargs):
            if len(args) >= 4 and args[1] == "snapshot" and args[2] == "delete":
                deleted_names.append(args[4])
            return (0, "", "")

        with (
            patch.object(manager, "_list_snapshots_with_meta", return_value=snapshots),
            patch("genesis.guardian.snapshots._run_subprocess", track_delete),
        ):
            await manager.prune()
        assert "guardian-1-healthy" not in deleted_names

    @pytest.mark.asyncio
    async def test_prune_deletes_stale_newest_pre_recovery(
        self, manager: SnapshotManager,
    ) -> None:
        """The exact incident: a guardian-pre-recovery snapshot sorts as 'newest'
        (name suffix), so retention alone protects it forever. Age-prune must
        delete it because it is stale AND not the latest-healthy lifeline —
        while keeping the older healthy snapshot as the rollback lifeline."""
        snapshots = _meta([
            ("guardian-20260503-120000-pre-recovery", 61.0),  # stale, sorts newest
            ("guardian-20260401-120000-healthy", 90.0),       # older, the lifeline
        ])
        # Sanity: sorted newest-first by name, pre-recovery is index 0.
        assert snapshots[0][0].endswith("-pre-recovery")
        deleted_names: list[str] = []

        async def track_delete(*args, **kwargs):
            if len(args) >= 4 and args[1] == "snapshot" and args[2] == "delete":
                deleted_names.append(args[4])
            return (0, "", "")

        with (
            patch.object(manager, "_list_snapshots_with_meta", return_value=snapshots),
            patch("genesis.guardian.snapshots._run_subprocess", track_delete),
        ):
            await manager.prune()
        assert "guardian-20260503-120000-pre-recovery" in deleted_names
        assert "guardian-20260401-120000-healthy" not in deleted_names

    @pytest.mark.asyncio
    async def test_prune_keeps_aged_healthy_lifeline(self, manager: SnapshotManager) -> None:
        """Even if the ONLY healthy snapshot is older than max_age_days, it must
        survive — it is the offline snapshot-rollback lifeline."""
        snapshots = _meta([
            ("guardian-20260401-healthy", 90.0),  # 90d old but the only healthy
        ])
        deleted_names: list[str] = []

        async def track_delete(*args, **kwargs):
            if len(args) >= 4 and args[1] == "snapshot" and args[2] == "delete":
                deleted_names.append(args[4])
            return (0, "", "")

        with (
            patch.object(manager, "_list_snapshots_with_meta", return_value=snapshots),
            patch("genesis.guardian.snapshots._run_subprocess", track_delete),
        ):
            await manager.prune()
        assert deleted_names == []


class TestSnapshotExpiryPolicy:

    @pytest.mark.asyncio
    async def test_enforce_expiry_sets_incus_config(self, manager: SnapshotManager) -> None:
        """enforce_expiry_policy sets snapshots.expiry (scheduled-only) on the container."""
        calls: list[tuple] = []

        async def record(*args, **kwargs):
            calls.append(args)
            return (0, "", "")

        with patch("genesis.guardian.snapshots._run_subprocess", record):
            ok = await manager.enforce_expiry_policy()
        assert ok is True
        assert any(
            a[:4] == ("incus", "config", "set", manager._container)
            and "snapshots.expiry" in a
            for a in calls
        )
        # Must NOT set snapshots.expiry.manual (would expire user snapshots).
        assert not any("snapshots.expiry.manual" in a for a in calls)


class TestMarkHealthy:

    @pytest.mark.asyncio
    async def test_mark_healthy(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_smart(snapshot_rc=0),
        ):
            name = await manager.mark_healthy()
        assert name is not None
        assert name.endswith("-healthy")


def _mock_subprocess_headroom(
    total_bytes: int = 300 * 1024**3,
    free_bytes: int = 100 * 1024**3,
    snapshot_rc: int = 0,
    post_free_bytes: int | None = None,
):
    """Mock that returns byte-level pool info for headroom tests."""
    state = {"snapshot_created": False}

    async def mock(*args, **kwargs):
        cmd = args[0] if args else ""
        all_args = " ".join(str(a) for a in args)
        if cmd == "incus" and len(args) > 3 and args[1] == "config":
            return (0, "genesis-pool\n", "")
        if cmd == "df" and "--block-size=1" in all_args:
            # After snapshot create, return post_free_bytes
            if state["snapshot_created"] and post_free_bytes is not None:
                return (0, f"1B-blocks Avail\n{total_bytes} {post_free_bytes}\n", "")
            return (0, f"1B-blocks Avail\n{total_bytes} {free_bytes}\n", "")
        if cmd == "df":
            # Percentage-based (check_pool_space fallback)
            pct = int((1 - free_bytes / total_bytes) * 100) if total_bytes else 100
            return (0, f"Use%\n {pct}%\n", "")
        if cmd == "incus" and len(args) > 2 and args[1] == "snapshot":
            if args[2] == "list":
                return (0, "[]", "")
            if args[2] == "create":
                state["snapshot_created"] = True
            return (snapshot_rc, "", "")
        return (snapshot_rc, "", "")
    return mock


class TestHeadroomGating:
    """Tests for headroom-based snapshot gating."""

    @pytest.mark.asyncio
    async def test_no_history_requires_10pct_free(self, manager: SnapshotManager) -> None:
        """Without snapshot size history, require 10% of pool free."""
        # 300GB pool, 100GB free = 33% free → should pass
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_headroom(
                total_bytes=300 * 1024**3, free_bytes=100 * 1024**3,
            ),
        ):
            ok = await manager.safe_to_snapshot(snapshot_size_history=[])
        assert ok is True

    @pytest.mark.asyncio
    async def test_no_history_rejects_low_free(self, manager: SnapshotManager) -> None:
        """Without history, reject if < 10% free."""
        # 300GB pool, 5GB free = 1.7% → should fail
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_headroom(
                total_bytes=300 * 1024**3, free_bytes=5 * 1024**3,
            ),
        ):
            ok = await manager.safe_to_snapshot(snapshot_size_history=[])
        assert ok is False

    @pytest.mark.asyncio
    async def test_with_history_uses_headroom(self, manager: SnapshotManager) -> None:
        """With history, require free > max(5GB, 2x avg last 3 snapshots)."""
        # History: 3 snapshots averaging 2GB each → need max(5GB, 4GB) = 5GB
        # 300GB pool, 10GB free → should pass (10GB > 5GB)
        history = [2 * 1024**3, 2 * 1024**3, 2 * 1024**3]
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_headroom(
                total_bytes=300 * 1024**3, free_bytes=10 * 1024**3,
            ),
        ):
            ok = await manager.safe_to_snapshot(snapshot_size_history=history)
        assert ok is True

    @pytest.mark.asyncio
    async def test_with_history_rejects_tight_headroom(self, manager: SnapshotManager) -> None:
        """Reject when free space < required headroom."""
        # History: 3 snapshots averaging 10GB each → need max(5GB, 20GB) = 20GB
        # 300GB pool, 15GB free → should fail (15GB < 20GB)
        history = [10 * 1024**3, 10 * 1024**3, 10 * 1024**3]
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_headroom(
                total_bytes=300 * 1024**3, free_bytes=15 * 1024**3,
            ),
        ):
            ok = await manager.safe_to_snapshot(snapshot_size_history=history)
        assert ok is False

    @pytest.mark.asyncio
    async def test_pool_detection_failure_falls_back_to_percentage(
        self, manager: SnapshotManager,
    ) -> None:
        """If _get_pool_free_bytes returns None, fall back to old pct check."""
        call_count = {"pool": 0}

        async def mock(*args, **kwargs):
            cmd = args[0] if args else ""
            all_args = " ".join(str(a) for a in args)
            if cmd == "incus" and len(args) > 3 and args[1] == "config":
                call_count["pool"] += 1
                if "--block-size=1" in all_args or call_count["pool"] <= 1:
                    # First pool detection (for _get_pool_free_bytes) fails
                    return (1, "", "error")
                # Second pool detection (for check_pool_space) succeeds
                return (0, "genesis-pool\n", "")
            if cmd == "df":
                return (0, "Use%\n 20%\n", "")
            return (0, "", "")

        with patch("genesis.guardian.snapshots._run_subprocess", mock):
            ok = await manager.safe_to_snapshot(snapshot_size_history=[1024])
        assert ok is True

    @pytest.mark.asyncio
    async def test_take_records_size_to_history(self, manager: SnapshotManager) -> None:
        """After successful take(), snapshot size should be appended to history."""
        history: list[int] = []
        free_before = 100 * 1024**3
        free_after = 98 * 1024**3  # 2GB snapshot
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess_headroom(
                total_bytes=300 * 1024**3,
                free_bytes=free_before,
                post_free_bytes=free_after,
            ),
        ):
            name = await manager.take(label="test", snapshot_size_history=history)
        assert name is not None
        assert len(history) == 1
        assert history[0] == free_before - free_after


class TestGetLatestHealthy:

    @pytest.mark.asyncio
    async def test_get_latest_healthy(self, manager: SnapshotManager) -> None:
        with patch.object(
            manager, "list_snapshots",
            return_value=[
                "guardian-20260325-120000",
                "guardian-20260325-100000-healthy",
                "guardian-20260324-120000-healthy",
            ],
        ):
            name = await manager.get_latest_healthy()
        assert name == "guardian-20260325-100000-healthy"

    @pytest.mark.asyncio
    async def test_no_healthy_snapshot(self, manager: SnapshotManager) -> None:
        with patch.object(
            manager, "list_snapshots",
            return_value=["guardian-20260325-120000"],
        ):
            name = await manager.get_latest_healthy()
        assert name is None
