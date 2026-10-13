"""Tests for Guardian snapshot manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.config import GuardianConfig
from genesis.guardian.snapshots import (
    REFUSED_OTHER,
    REFUSED_POOL_GATE,
    REFUSED_POOL_SPACE,
    REFUSED_PROBE,
    SnapshotManager,
    _created_from_name,
)


@pytest.fixture
def config(tmp_path) -> GuardianConfig:
    # Never the real state dir.
    cfg = GuardianConfig()
    cfg.state_dir = str(tmp_path / "guardian-state")
    return cfg


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
            name = await manager.take(label="pre-recovery")
        assert name is not None
        assert name.startswith("guardian-")
        assert name.endswith("-pre-recovery")

    @pytest.mark.asyncio
    async def test_take_refuses_an_unknown_label(self, manager: SnapshotManager) -> None:
        # A label the ownership pattern cannot see would make a snapshot the
        # guardian never prunes, rotates or frees — refused at the source.
        run = AsyncMock()
        with patch("genesis.guardian.snapshots._run_subprocess", run), pytest.raises(ValueError):
            await manager.take(label="test")
        run.assert_not_called()

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
        assert name.count("-") == 2  # guardian-YYYYmmdd-HHMMSS, no label

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
    async def test_prune_over_retention(self, config: GuardianConfig) -> None:
        """Should prune oldest snapshots past retention."""
        config.snapshots.retention = 1
        manager = SnapshotManager(config)
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
            name = await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True)
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
            name = await manager.take(label="pre-recovery", snapshot_size_history=history)
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


def _pool_status(detected: bool = True, data=None, meta=None):
    """A NAMED LVM-thin pool measurement (an unnamed one is refused outright)."""
    from genesis.guardian.pool import StoragePoolStatus
    return StoragePoolStatus(
        detected=detected, data_pct=data, metadata_pct=meta, pool_size_bytes=100 * 1024**3,
        pool_name="default", vg_name="vg0", thinpool_lv="IncusThinPool",
    )


class TestLvmPoolGating:
    """safe_to_snapshot must gate on REAL thin-pool allocation when LVM.

    The df-on-mountpath fallback measures the host rootfs on LVM backends
    (the original-incident blindness), so when measure_storage_pool detects
    the pool, its data%/metadata% are authoritative.
    """

    @pytest.mark.asyncio
    async def test_refuses_when_data_at_high_tier(
        self, manager: SnapshotManager,
    ) -> None:
        with patch(
            "genesis.guardian.snapshots.measure_storage_pool",
            return_value=_pool_status(data=86.0, meta=10.0),
        ):
            assert await manager.safe_to_snapshot() is False

    @pytest.mark.asyncio
    async def test_refuses_when_metadata_at_high_tier(
        self, manager: SnapshotManager,
    ) -> None:
        # Metadata exhaustion is nastier than data — gate at its (lower) tier.
        with patch(
            "genesis.guardian.snapshots.measure_storage_pool",
            return_value=_pool_status(data=40.0, meta=71.0),
        ):
            assert await manager.safe_to_snapshot() is False

    @pytest.mark.asyncio
    async def test_allows_below_tiers(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots.measure_storage_pool",
            return_value=_pool_status(data=55.0, meta=29.0),
        ):
            assert await manager.safe_to_snapshot() is True

    @pytest.mark.asyncio
    async def test_falls_back_to_df_when_pool_undetected(
        self, manager: SnapshotManager,
    ) -> None:
        """Non-LVM/undetectable pool → existing df byte-headroom logic."""
        async def df_mock(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "incus":
                return (0, "genesis-pool\n", "")
            if cmd == "df":
                # size, avail (bytes): 100G total, 50G free — plenty
                return (0, "1B-blocks Avail\n107374182400 53687091200\n", "")
            return (0, "", "")
        with (
            patch(
                "genesis.guardian.snapshots.measure_storage_pool",
                return_value=_pool_status(detected=False),
            ),
            patch("genesis.guardian.snapshots._run_subprocess", df_mock),
        ):
            assert await manager.safe_to_snapshot() is True


def _take_env_mock(existing_json: str, deletes: list, creates: list):
    """Subprocess mock for take(): list/delete/create capture."""
    async def mock(*args, **kwargs):
        cmd = args[0] if args else ""
        if cmd == "incus" and args[1] == "config":
            return (0, "genesis-pool\n", "")
        if cmd == "df":
            return (0, "1B-blocks Avail\n107374182400 53687091200\n", "")
        if cmd == "incus" and args[1] == "snapshot" and args[2] == "list":
            return (0, existing_json, "")
        if cmd == "incus" and args[1] == "snapshot" and args[2] == "delete":
            deletes.append(args[4])
            return (0, "", "")
        if cmd == "incus" and args[1] == "snapshot" and args[2] == "create":
            creates.append(args[4])
            return (0, "", "")
        return (0, "", "")
    return mock


class TestTakeHealthyExemption:
    """take()'s delete-before-create must never evict the healthy lifeline.

    prune() got the latest-healthy exemption; take()'s parallel eviction path
    was missed — at retention=1 a pre-recovery take would delete the healthy
    snapshot RIGHT BEFORE the risky action that might need it.
    """

    @pytest.mark.asyncio
    async def test_pre_recovery_take_does_not_evict_healthy(
        self, config: GuardianConfig,
    ) -> None:
        config.snapshots.retention = 1
        manager = SnapshotManager(config)
        existing = json.dumps([
            {"name": "guardian-20260702-000000-pre-recovery",
             "created_at": "2026-07-02T00:00:00Z"},
            {"name": "guardian-20260701-000000-healthy",
             "created_at": "2026-07-01T00:00:00Z"},
        ])
        deletes: list = []
        creates: list = []
        with (
            patch(
                "genesis.guardian.snapshots.measure_storage_pool",
                return_value=_pool_status(data=50.0, meta=20.0),
            ),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _take_env_mock(existing, deletes, creates),
            ),
        ):
            name = await manager.take(label="pre-recovery")
        assert name is not None
        assert "guardian-20260701-000000-healthy" not in deletes
        # The older non-healthy IS evicted to hold retention.
        assert "guardian-20260702-000000-pre-recovery" in deletes


class TestMarkHealthyRotation:
    """mark_healthy: create the NEW healthy first, then delete superseded
    healthy snapshots — no zero-lifeline window, create-failure keeps the old."""

    @pytest.mark.asyncio
    async def test_rotation_deletes_only_superseded_healthy(
        self, config: GuardianConfig,
    ) -> None:
        config.snapshots.retention = 2
        manager = SnapshotManager(config)
        existing = json.dumps([
            {"name": "guardian-20260702-000000-pre-recovery",
             "created_at": "2026-07-02T00:00:00Z"},
            {"name": "guardian-20260701-000000-healthy",
             "created_at": "2026-07-01T00:00:00Z"},
        ])
        deletes: list = []
        creates: list = []
        with (
            patch(
                "genesis.guardian.snapshots.measure_storage_pool",
                return_value=_pool_status(data=50.0, meta=20.0),
            ),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _take_env_mock(existing, deletes, creates),
            ),
        ):
            name = await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True)
        assert name is not None and name.endswith("-healthy")
        assert "guardian-20260701-000000-healthy" in deletes
        assert "guardian-20260702-000000-pre-recovery" not in deletes

    @pytest.mark.asyncio
    async def test_create_failure_keeps_old_healthy(
        self, config: GuardianConfig,
    ) -> None:
        manager = SnapshotManager(config)
        existing = json.dumps([
            {"name": "guardian-20260701-000000-healthy",
             "created_at": "2026-07-01T00:00:00Z"},
        ])
        deletes: list = []

        async def failing_create(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "incus" and args[1] == "config":
                return (0, "genesis-pool\n", "")
            if cmd == "df":
                return (0, "1B-blocks Avail\n107374182400 53687091200\n", "")
            if cmd == "incus" and args[1] == "snapshot" and args[2] == "list":
                return (0, existing, "")
            if cmd == "incus" and args[1] == "snapshot" and args[2] == "delete":
                deletes.append(args[4])
                return (0, "", "")
            if cmd == "incus" and args[1] == "snapshot" and args[2] == "create":
                return (1, "", "boom")
            return (0, "", "")

        with (
            patch(
                "genesis.guardian.snapshots.measure_storage_pool",
                return_value=_pool_status(data=50.0, meta=20.0),
            ),
            patch("genesis.guardian.snapshots._run_subprocess", failing_create),
        ):
            name = await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True)
        assert name is None
        assert deletes == []  # old lifeline untouched


class TestDelete:

    @pytest.mark.asyncio
    async def test_delete_success(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess", _mock_subprocess(0),
        ):
            assert await manager.delete("guardian-20260701-000000") is True

    @pytest.mark.asyncio
    async def test_delete_failure(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(1, "", "nope"),
        ):
            assert await manager.delete("guardian-20260701-000000") is False


# --- pool-refusal awareness + delete-first rotation ---------------------------

_LVM_THRESHOLD_ERR = (
    "Error: Create instance snapshot: Error creating LVM logical volume snapshot: "
    "Failed to run: lvcreate ... exit status 5 (Cannot create new thin volume, free "
    "space in thin pool vg0/IncusThinPool reached threshold.)"
)


class _FakeIncus:
    """incus snapshot list/create/delete over an in-memory table."""

    def __init__(
        self, snaps: dict[str, str], create_err: str | None = None, *, fail_all: bool = False,
        until_delete: bool = False,
    ) -> None:
        self.snaps = dict(snaps)  # name -> created_at (RFC3339)
        self.create_err = create_err
        self.fail_all = fail_all
        # A pool refused BECAUSE of the lifeline keeps refusing until one goes.
        self.until_delete = until_delete
        self.creates = 0
        self.deleted: list[str] = []

    async def run(self, *args, **kwargs):
        if args[:3] == ("incus", "snapshot", "list"):
            return 0, json.dumps([{"name": n, "created_at": c} for n, c in self.snaps.items()]), ""
        if args[:3] == ("incus", "snapshot", "delete"):
            self.deleted.append(args[4])
            self.snaps.pop(args[4], None)
            return 0, "", ""
        if args[:3] == ("incus", "snapshot", "create"):
            self.creates += 1
            refuse = self.creates == 1 or self.fail_all or (self.until_delete and not self.deleted)
            if self.create_err and refuse:
                return 1, "", self.create_err
            self.snaps[args[4]] = datetime.now(UTC).isoformat()
            return 0, "", ""
        return 0, "", ""


def _ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class TestRefusalAndDeleteFirst:

    def test_created_from_name(self) -> None:
        got = _created_from_name("guardian-20260919-162609-healthy", "guardian-")
        assert got == datetime(2026, 9, 19, 16, 26, 9, tzinfo=UTC)
        assert _created_from_name("guardian-manual", "guardian-") is None
        # Anchored after the prefix: a timestamp-shaped PREFIX is not the date.
        pre = "archive-20200101-000000-"
        assert _created_from_name(pre + "20260919-162609-healthy", pre) == got
        assert _created_from_name(pre + "healthy", pre) is None
        assert _created_from_name("other-20260919-162609", "guardian-") is None

    @pytest.mark.asyncio
    async def test_take_classifies_lvm_threshold_as_pool_space(self, manager) -> None:
        fake = _FakeIncus({}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch("genesis.guardian.snapshots._run_subprocess", fake.run),
        ):
            assert await manager.take(label="pre-recovery") is None
        assert manager.last_refusal == REFUSED_POOL_SPACE

    @pytest.mark.asyncio
    async def test_take_classifies_other_failures(self, manager) -> None:
        fake = _FakeIncus({}, create_err="Error: instance not found")
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch("genesis.guardian.snapshots._run_subprocess", fake.run),
        ):
            assert await manager.take(label="pre-recovery") is None
        assert manager.last_refusal == REFUSED_OTHER

    @pytest.mark.asyncio
    async def test_take_records_pool_gate(self, manager) -> None:
        async def refuse_measured(*_a, **_k):
            manager.last_gate_measured = True
            return False

        with patch.object(manager, "safe_to_snapshot", refuse_measured):
            assert await manager.take(label="pre-recovery") is None
        assert manager.last_refusal == REFUSED_POOL_GATE

    @pytest.mark.asyncio
    async def test_strict_list_is_none_on_failure_and_falls_back_to_name_time(
        self, manager,
    ) -> None:
        async def fail(*a, **k):
            return 1, "", "incus: daemon unreachable"

        with patch("genesis.guardian.snapshots._run_subprocess", fail):
            assert await manager.list_snapshot_meta_strict() is None

        async def ok(*a, **k):
            return 0, json.dumps([
                {"name": "guardian-20260919-162609-healthy"},
                {"name": "someone-elses"},
            ]), ""

        with patch("genesis.guardian.snapshots._run_subprocess", ok):
            got = await manager.list_snapshot_meta_strict()
        assert got == [
            ("guardian-20260919-162609-healthy", datetime(2026, 9, 19, 16, 26, 9, tzinfo=UTC)),
        ]


def _lvm(used_frac: float, size: int = 100 * 1024**3, **kw):
    from genesis.guardian.pool import StoragePoolStatus

    base = dict(
        detected=True, data_pct=used_frac * 100, metadata_pct=40.0, pool_size_bytes=size,
        pool_name="default", vg_name="vg0", thinpool_lv="IncusThinPool",
    )
    base.update(kw)
    return StoragePoolStatus(**base)


_POOL = 100 * 1024**3
_CT_LV = 90 * 1024**3


def _with_thin_lvs(
    fake, manager, *, live_frac: float, extra_rows=(), pool_frac=None, drop_container=False,
):
    """Wrap a _FakeIncus so the one-read `lvs` answers like a real host: the
    thin-pool row (``pool_frac`` used; defaults to what the patched
    measurement says), the container's own LV mapping ``live_frac`` of the
    pool, every guardian snapshot in ``fake.snaps`` inactive (blank data%)."""
    from genesis.guardian.pool import THIN_LV_SELECT, incus_snapshot_lv_name

    async def run(*args, **kwargs):
        if "lvs" in args and THIN_LV_SELECT in args:
            import genesis.guardian.snapshots as snap_mod

            used = pool_frac
            if used is None:
                used = (await snap_mod.measure_storage_pool(None)).data_pct / 100
            rows = [{
                "lv_name": "IncusThinPool", "pool_lv": "", "segtype": "thin-pool",
                "data_percent": f"{100 * used:.2f}", "lv_size": str(_POOL),
            }]
            if not drop_container:
                rows.append({
                    "lv_name": f"containers_{manager._container}", "pool_lv": "IncusThinPool",
                    "segtype": "thin",
                    "data_percent": f"{100 * live_frac * _POOL / _CT_LV:.2f}",
                    "lv_size": str(_CT_LV),
                })
            rows += [{
                "lv_name": incus_snapshot_lv_name(manager._container, n),
                "pool_lv": "IncusThinPool", "segtype": "thin", "data_percent": "",
                "lv_size": str(_CT_LV),
            } for n in fake.snaps]
            rows += list(extra_rows)
            return 0, json.dumps({"report": [{"lv": rows}]}), ""
        return await fake.run(*args, **kwargs)

    return run


class TestDeleteFirstRotation:
    """Delete-first needs ALL of: a MEASURED pool refusal, a lifeline at least
    a rotation old, LVM evidence that the healthy snapshots hold space no live
    volume maps, and the caller's permission (relief live, settle reserved)."""

    OLD = "guardian-20260101-000000-healthy"

    async def _run(
        self, manager, *, snaps, err, now_frac, live_frac=0.80, allowed=True,
        reserve=None, extra_rows=(), status=None,
    ):
        fake = _FakeIncus(snaps, create_err=err, until_delete=True)
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _with_thin_lvs(fake, manager, live_frac=live_frac, extra_rows=extra_rows),
            ),
            patch(
                "genesis.guardian.snapshots.measure_storage_pool",
                return_value=status or _lvm(now_frac),
            ),
        ):
            name = await manager.mark_healthy(
                delete_first_allowed=allowed, reserve_settle=reserve, healthy_confirmed=True,
            )
        return name, fake

    @pytest.mark.asyncio
    async def test_snapshot_held_space_is_deleted_first(self, manager) -> None:
        # pool 85 GiB used, the live container maps 80: snapshots hold >= 5.
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(30)}, err=_LVM_THRESHOLD_ERR, now_frac=0.85,
        )
        assert name is not None and name.endswith("-healthy")
        assert fake.deleted[0] == self.OLD
        assert "no live volume maps" in (manager.last_rotation_note or "")

    @pytest.mark.asyncio
    async def test_pressure_cleared_before_the_delete_rotates_create_first(self, manager) -> None:
        """Review: the refusal was measured before the lifeline reads, and was
        trusted through the delete. When the pool accepts a create by then
        (an autoextend landed), the old lifeline must go only AFTER its
        replacement exists — never first."""
        fake = _FakeIncus({self.OLD: _ago(30)}, create_err=_LVM_THRESHOLD_ERR)  # refuses once
        order: list[str] = []
        inner = _with_thin_lvs(fake, manager, live_frac=0.80)

        async def run(*args, **kwargs):
            if args[:3] in (("incus", "snapshot", "create"), ("incus", "snapshot", "delete")):
                order.append(args[2])
            return await inner(*args, **kwargs)

        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch("genesis.guardian.snapshots._run_subprocess", run),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
        ):
            name = await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True)
        assert name is not None and name.endswith("-healthy")
        assert order == ["create", "create", "delete"] and fake.deleted == [self.OLD]
        assert manager.last_rotation_deleted is False  # an ordinary rotation, not delete-first
        assert "deleted first" not in (manager.last_rotation_note or "")

    @pytest.mark.parametrize("retry", ["create_error", "probe_failure"])
    @pytest.mark.asyncio
    async def test_retry_refused_for_a_non_pool_reason_keeps_the_lifeline(
        self, manager, retry,
    ) -> None:
        """Internal review: only a POOL refusal of the pre-delete retry licenses
        the delete. A retry that failed for any other reason — the create erred
        (not a pool-space message), or the gate's probe could not measure —
        says nothing about the pool, so the lifeline stays."""
        fake = _FakeIncus({self.OLD: _ago(30)}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
        inner = _with_thin_lvs(fake, manager, live_frac=0.80)
        gate_calls = 0

        async def run(*args, **kwargs):
            if retry == "create_error" and args[:3] == ("incus", "snapshot", "create") and fake.creates:
                fake.creates += 1
                return 1, "", "Error: instance not found"
            return await inner(*args, **kwargs)

        async def gate(*_a, **_k):
            nonlocal gate_calls
            gate_calls += 1
            if retry == "probe_failure" and gate_calls == 2:
                manager.last_gate_measured = False
                return False
            return True

        with (
            patch.object(manager, "safe_to_snapshot", side_effect=gate),
            patch("genesis.guardian.snapshots._run_subprocess", run),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
        ):
            name = await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True)
        assert name is None and fake.deleted == [] and self.OLD in fake.snaps
        assert manager.last_refusal == (REFUSED_OTHER if retry == "create_error" else REFUSED_PROBE)
        assert gate_calls == 2  # the retry really ran

    @pytest.mark.asyncio
    async def test_retry_that_evicted_defers_the_delete(self, manager) -> None:
        """Internal review: a retry whose retention eviction deleted a snapshot
        must not also delete the lifeline — at most one delete per attempt, the
        freed space may still be coming back."""
        pre = "guardian-20251231-000000-pre-recovery"
        fake = _FakeIncus({self.OLD: _ago(30), pre: _ago(40)}, create_err=_LVM_THRESHOLD_ERR)
        manager._retention = 1
        gate_calls = 0

        async def gate(*_a, **_k):
            nonlocal gate_calls
            gate_calls += 1
            if gate_calls == 1:  # the first attempt: refused on a real measurement
                manager.last_gate_measured = True
                return False
            return True

        with (
            patch.object(manager, "safe_to_snapshot", side_effect=gate),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _with_thin_lvs(fake, manager, live_frac=0.80),
            ),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
            patch("genesis.guardian.snapshots.snapshot_only_bytes", return_value=10 * 1024**3),
        ):
            name = await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True)
        assert name is None and fake.deleted == [pre] and self.OLD in fake.snaps

    @pytest.mark.asyncio
    async def test_report_without_the_container_is_no_evidence(self, manager) -> None:
        """Internal review: an lvs report missing the running container's own
        LV (empty, filtered, renamed) must not read as 'the snapshots hold the
        whole pool'."""
        fake = _FakeIncus({self.OLD: _ago(30)}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _with_thin_lvs(fake, manager, live_frac=0.1, drop_container=True),
            ),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.95)),
        ):
            assert await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True) is None
        assert fake.deleted == []

    @pytest.mark.asyncio
    async def test_bound_uses_the_same_reads_pool_row(self, manager) -> None:
        """Both terms from ONE lvs read: the earlier measurement says 95% but
        the per-volume read (the same instant as its own pool row) says the
        pool is at 80.5% with the container mapping 80% — nothing held."""
        fake = _FakeIncus({self.OLD: _ago(30)}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _with_thin_lvs(fake, manager, live_frac=0.80, pool_frac=0.805),
            ),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.95)),
        ):
            assert await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True) is None
        assert fake.deleted == []

    @pytest.mark.asyncio
    async def test_timed_out_delete_first_says_the_lifeline_may_be_gone(self, manager) -> None:
        fake = _FakeIncus({self.OLD: _ago(30)}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
        inner = _with_thin_lvs(fake, manager, live_frac=0.80)

        async def run(*args, **kwargs):
            if args[:3] == ("incus", "snapshot", "delete"):
                return -1, "", "timeout"
            return await inner(*args, **kwargs)

        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch("genesis.guardian.snapshots._run_subprocess", run),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
        ):
            assert await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True) is None
        # The first create and the pre-delete retry; none after an unconfirmed delete.
        assert fake.creates == 2
        assert "may be gone" in (manager.last_rotation_note or "")

    @pytest.mark.asyncio
    async def test_live_data_growth_keeps_the_lifeline(self, manager) -> None:
        """Devin's example: the pool grew because the CONTAINER wrote new data.
        The container maps nearly all of it, so the snapshot holds little and
        deleting it would free nothing."""
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(30)}, err=_LVM_THRESHOLD_ERR,
            now_frac=0.86, live_frac=0.855,
        )
        assert name is None and fake.deleted == []

    @pytest.mark.asyncio
    async def test_unattributable_volume_means_no_evidence(self, manager) -> None:
        # An operator's own snapshot (inactive, blank) is in the pool: what it
        # holds is not the lifeline's, so there is no measurement to act on.
        from genesis.guardian.pool import incus_snapshot_lv_name

        mine = {
            "lv_name": incus_snapshot_lv_name(manager._container, "my-snap"),
            "pool_lv": "IncusThinPool", "segtype": "thin", "data_percent": "",
            "lv_size": str(_CT_LV),
        }
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(30)}, err=_LVM_THRESHOLD_ERR, now_frac=0.95,
            extra_rows=[mine],
        )
        assert name is None and fake.deleted == []

    @pytest.mark.asyncio
    async def test_other_live_volumes_count_against_the_bound(self, manager) -> None:
        other = {
            "lv_name": "custom_default_scratch", "pool_lv": "IncusThinPool", "segtype": "thin",
            "data_percent": "50.00", "lv_size": str(10 * 1024**3),  # maps 5 GiB
        }
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(30)}, err=_LVM_THRESHOLD_ERR,
            now_frac=0.85, extra_rows=[other],  # 85 - 80 - 5 = 0 held
        )
        assert name is None and fake.deleted == []

    @pytest.mark.asyncio
    async def test_non_lvm_pool_never_deletes_first(self, manager) -> None:
        from genesis.guardian.pool import StoragePoolStatus

        btrfs = StoragePoolStatus(
            detected=True, pool_used_pct=95.0, pool_size_bytes=_POOL, pool_name="p",
        )
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(30)}, err=_LVM_THRESHOLD_ERR, now_frac=0.95,
            status=btrfs,
        )
        assert name is None and fake.deleted == []

    @pytest.mark.asyncio
    async def test_not_allowed_means_no_delete(self, manager) -> None:
        """Relief killed / alert_only / settling: delete-first stays off."""
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(30)}, err=_LVM_THRESHOLD_ERR, now_frac=0.95,
            allowed=False,
        )
        assert name is None and fake.deleted == []

    @pytest.mark.asyncio
    async def test_settle_is_reserved_before_the_delete(self, manager) -> None:
        order: list[str] = []
        fake_ref: dict = {}

        def reserve() -> bool:
            order.append(f"reserve deleted={list(fake_ref['fake'].deleted)}")
            return True

        fake = _FakeIncus({self.OLD: _ago(30)}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
        fake_ref["fake"] = fake
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _with_thin_lvs(fake, manager, live_frac=0.80),
            ),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
        ):
            await manager.mark_healthy(
                delete_first_allowed=True, reserve_settle=reserve, healthy_confirmed=True,
            )
        assert order == ["reserve deleted=[]"] and fake.deleted[0] == self.OLD

    @pytest.mark.asyncio
    async def test_unwritable_settle_forbids_the_delete(self, manager) -> None:
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(30)}, err=_LVM_THRESHOLD_ERR, now_frac=0.85,
            reserve=lambda: False,
        )
        assert name is None and fake.deleted == []

    @pytest.mark.asyncio
    async def test_fresh_lifeline_is_never_deleted_first(self, manager) -> None:
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(2)}, err=_LVM_THRESHOLD_ERR, now_frac=0.95,
        )
        assert name is None and fake.deleted == []

    @pytest.mark.asyncio
    async def test_non_pool_failure_never_deletes_first(self, manager) -> None:
        name, fake = await self._run(
            manager, snaps={self.OLD: _ago(30)}, err="Error: instance not found",
            now_frac=0.95,
        )
        assert name is None and fake.deleted == []
        assert manager.last_rotation_note is None

    @pytest.mark.asyncio
    async def test_probe_failure_is_not_a_pool_refusal(self, manager) -> None:
        """safe_to_snapshot also refuses when it could not measure (df/incus
        down). That is REFUSED_PROBE, and it never licenses delete-first."""
        fake = _FakeIncus({self.OLD: _ago(30)})

        async def refuse_unmeasured(*_a, **_k):
            manager.last_gate_measured = False
            return False

        with (
            patch.object(manager, "safe_to_snapshot", refuse_unmeasured),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _with_thin_lvs(fake, manager, live_frac=0.5),
            ),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.95)),
        ):
            assert await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True) is None
        assert manager.last_refusal == REFUSED_PROBE
        assert fake.deleted == []

    @pytest.mark.asyncio
    async def test_measured_gate_refusal_is_a_pool_refusal(self, manager) -> None:
        with patch(
            "genesis.guardian.snapshots.measure_storage_pool",
            return_value=_lvm(0.90),  # above the 85% data tier
        ):
            assert await manager.safe_to_snapshot() is False
        assert manager.last_gate_measured is True

    @pytest.mark.asyncio
    async def test_unnamed_pool_refusal_is_not_a_pool_refusal(self, manager) -> None:
        """Several thin pools in the VG: the figures may be another pool's."""
        with patch(
            "genesis.guardian.snapshots.measure_storage_pool",
            return_value=_lvm(0.90, thinpool_lv=None),
        ):
            assert await manager.safe_to_snapshot() is False
        assert manager.last_gate_measured is False

    @pytest.mark.asyncio
    async def test_delete_first_takes_the_oldest_healthy(self, manager) -> None:
        older = "guardian-20251230-000000-healthy"
        name, fake = await self._run(
            manager, snaps={older: _ago(50), self.OLD: _ago(26)}, err=_LVM_THRESHOLD_ERR,
            now_frac=0.85,
        )
        assert fake.deleted[0] == older


@pytest.mark.asyncio
async def test_strict_list_owns_only_generated_names(manager) -> None:
    """Automatic deletes act on this listing: a hand-made snapshot that merely
    starts with the prefix is not the guardian's (security review)."""
    rows = [
        {"name": "guardian-20260919-162609-healthy"},
        {"name": "guardian-20260919-162609"},
        {"name": "guardian-20260919-162609-pre-recovery"},
        {"name": "guardian-demo"},
        {"name": "guardian-onboarding-backup"},
        {"name": "guardian-20260919-162609 x"},
    ]

    async def ok(*a, **k):
        return 0, json.dumps(rows), ""

    with patch("genesis.guardian.snapshots._run_subprocess", ok):
        got = [n for n, _ in await manager.list_snapshot_meta_strict()]
    assert sorted(got) == sorted([
        "guardian-20260919-162609-healthy",
        "guardian-20260919-162609",
        "guardian-20260919-162609-pre-recovery",
    ])


class TestOwnershipChokepoint:
    """EVERY listing (prune, retention eviction, rotation, the rollback target)
    claims only guardian-GENERATED names — never a hand-made snapshot that
    merely starts with the prefix (review: 'guardian-mine-healthy' used to
    sort after the real lifeline and become the rollback target)."""

    LIFE = "guardian-20260919-162609-healthy"
    MINE = ("guardian-mine-healthy", "guardian-20260919-162609-mine", "guardian-demo")

    @pytest.mark.asyncio
    async def test_listing_and_rollback_target(self, manager) -> None:
        fake = _FakeIncus({self.LIFE: _ago(5), **{m: _ago(1) for m in self.MINE}})
        with patch("genesis.guardian.snapshots._run_subprocess", fake.run):
            assert await manager.list_snapshots() == [self.LIFE]
            assert await manager.get_latest_healthy() == self.LIFE

    @pytest.mark.asyncio
    async def test_prune_and_rotation_never_delete_hand_made(self, config) -> None:
        config.snapshots.max_age_days = 0  # everything is "stale"
        config.snapshots.retention = 1
        manager = SnapshotManager(config)
        fake = _FakeIncus({self.LIFE: _ago(50), **{m: _ago(900) for m in self.MINE}})
        with (
            patch("genesis.guardian.snapshots._run_subprocess", fake.run),
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch(
                "genesis.guardian.snapshots.measure_storage_pool",
                return_value=_lvm(0.5),
            ),
        ):
            await manager.prune()
            await manager.mark_healthy()
        assert not set(fake.deleted) & set(self.MINE), fake.deleted
        assert self.LIFE in fake.deleted  # the real lifeline did rotate

    @pytest.mark.asyncio
    async def test_empty_prefix_claims_nothing(self, config) -> None:
        config.snapshots.prefix = ""
        manager = SnapshotManager(config)
        fake = _FakeIncus({"20260919-162609-healthy": _ago(5)})
        with patch("genesis.guardian.snapshots._run_subprocess", fake.run):
            assert await manager.list_snapshots() == []


def _non_lvm(used_pct: float):
    from genesis.guardian.pool import StoragePoolStatus

    return StoragePoolStatus(
        detected=True, pool_used_pct=used_pct, pool_size_bytes=300 * 1024**3, pool_name="p",
    )


class TestGateMeasuredAcrossBackends:
    """A headroom refusal on a POSITIVELY non-LVM pool is a real pool refusal
    (so delete-first works on btrfs/dir); on an LVM pool it is df of the host
    filesystem and stays a probe refusal. And the gate is never looser than
    relief's reserve."""

    @pytest.mark.asyncio
    async def test_non_lvm_headroom_refusal_is_measured(self, manager) -> None:
        with (
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_non_lvm(95.0)),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _mock_subprocess_headroom(free_bytes=1 * 1024**3),
            ),
        ):
            assert await manager.safe_to_snapshot() is False
        assert manager.last_gate_measured is True

    @pytest.mark.asyncio
    async def test_lvm_with_blank_percents_df_refusal_is_a_probe(self, manager) -> None:
        from genesis.guardian.pool import StoragePoolStatus

        blank = StoragePoolStatus(
            detected=True, pool_name="default", vg_name="vg0", thinpool_lv="IncusThinPool",
        )
        with (
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=blank),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _mock_subprocess_headroom(free_bytes=1 * 1024**3),
            ),
        ):
            assert await manager.safe_to_snapshot() is False
        assert manager.last_gate_measured is False

    @pytest.mark.asyncio
    async def test_gate_refuses_inside_relief_reserve(self, manager) -> None:
        # 97.5% used on a large pool, but df says ample absolute headroom: the
        # headroom gate alone would admit a snapshot relief then deletes.
        with (
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_non_lvm(97.5)),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _mock_subprocess_headroom(free_bytes=100 * 1024**3),
            ),
        ):
            assert await manager.safe_to_snapshot() is False
        assert manager.last_gate_measured is True

    @pytest.mark.asyncio
    async def test_control_room_is_admitted(self, manager) -> None:
        with (
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_non_lvm(60.0)),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _mock_subprocess_headroom(free_bytes=100 * 1024**3),
            ),
        ):
            assert await manager.safe_to_snapshot() is True


@pytest.mark.asyncio
async def test_refused_retry_note_names_the_surviving_lifeline(manager) -> None:
    """Two healthy snapshots: delete-first removes the OLDER; if the retry is
    refused too, the newer still stands and the note must say so."""
    older, newer = "guardian-20251230-000000-healthy", "guardian-20260101-000000-healthy"
    fake = _FakeIncus(
        {older: _ago(50), newer: _ago(26)}, create_err=_LVM_THRESHOLD_ERR, fail_all=True,
    )
    with (
        patch.object(manager, "safe_to_snapshot", return_value=True),
        patch(
            "genesis.guardian.snapshots._run_subprocess",
            _with_thin_lvs(fake, manager, live_frac=0.80),
        ),
        patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
    ):
        assert await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True) is None
    assert fake.deleted == [older]
    assert f"rollback falls back to {newer}" in manager.last_rotation_note


class TestChronologyIsCreationTimeNotName:
    """Security review: a name fitting the ownership pattern can carry any
    date (``guardian-99999999-999999-healthy``). Every "newest healthy"
    decision must follow incus's created_at, or such a snapshot outranks the
    real lifeline — prune deletes the lifeline, rollback restores the forgery,
    and delete-first sees a "fresh" lifeline and never fires."""

    REAL = "guardian-20260925-120000-healthy"
    FORGED = "guardian-99999999-999999-healthy"

    @pytest.mark.asyncio
    async def test_rollback_target_and_prune(self, config) -> None:
        config.snapshots.retention = 1
        manager = SnapshotManager(config)
        fake = _FakeIncus({
            self.REAL: _ago(20),
            self.FORGED: _ago(200),  # created long BEFORE the real lifeline
            "guardian-99999990-999999": _ago(300),
        })
        with patch("genesis.guardian.snapshots._run_subprocess", fake.run):
            assert await manager.get_latest_healthy() == self.REAL
            await manager.prune()
        assert self.REAL not in fake.deleted, fake.deleted

    @pytest.mark.asyncio
    async def test_forged_name_cannot_suppress_delete_first(self, manager) -> None:
        # The real lifeline is 30h old; a forged "newest" one was created
        # minutes ago. By creation time the newest healthy is the forgery, so
        # it is fresh and delete-first rightly waits — but the forgery must not
        # masquerade as newer than it is: with it created BEFORE the real one,
        # the real lifeline is the newest and is nominated.
        fake = _FakeIncus({self.REAL: _ago(30), self.FORGED: _ago(60)})
        with patch("genesis.guardian.snapshots._run_subprocess", fake.run):
            stale = await manager._stale_lifeline(datetime.now(UTC))
        assert stale == self.FORGED  # the OLDEST healthy by creation time goes first
        meta = None
        with patch("genesis.guardian.snapshots._run_subprocess", fake.run):
            meta = await manager.list_snapshot_meta_strict()
        assert [n for n, _ in meta][0] == self.REAL



@pytest.mark.asyncio
async def test_retention_never_evicts_a_healthy_snapshot(config) -> None:
    """Review: at retention 2 with two healthy snapshots, take() used to evict
    the older BEFORE a create LVM then refused, and delete-first took the
    other — no rollback target left. Healthy snapshots are rotation's alone."""
    config.snapshots.retention = 1
    manager = SnapshotManager(config)
    a, b = "guardian-20260101-000000-healthy", "guardian-20260102-000000-healthy"
    pre = "guardian-20260103-000000-pre-recovery"
    fake = _FakeIncus({a: _ago(50), b: _ago(26), pre: _ago(20)}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
    with (
        patch.object(manager, "safe_to_snapshot", return_value=True),
        patch("genesis.guardian.snapshots._run_subprocess", fake.run),
    ):
        assert await manager.take(label="healthy") is None
    assert a not in fake.deleted and b not in fake.deleted
    assert fake.deleted == [pre]  # retention still applies to non-healthy ones



class TestReviewRound2:
    """External review round 2 on the core PR."""

    @pytest.mark.parametrize("stderr", [
        "Error: no space left on device",           # host fs / daemon ENOSPC
        "Error: insufficient free space for the database",
    ])
    @pytest.mark.asyncio
    async def test_generic_enospc_is_not_a_pool_refusal(self, manager, stderr) -> None:
        fake = _FakeIncus({}, create_err=stderr)
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch("genesis.guardian.snapshots._run_subprocess", fake.run),
        ):
            assert await manager.take(label="pre-recovery") is None
        assert manager.last_refusal == REFUSED_OTHER

    @pytest.mark.parametrize("status_kw", [
        dict(data_pct=10.0, metadata_pct=5.0, thinpool_lv=None),   # low % of an unnamed pool
        dict(data_pct=None, metadata_pct=None),                    # no figures at all
    ])
    @pytest.mark.asyncio
    async def test_gate_refuses_unattributable_lvm_as_probe(self, manager, status_kw) -> None:
        calls: list = []

        async def run(*a, **k):
            calls.append(a)
            return 0, "      Avail    1B-blocks\n90000000000 100000000000\n", ""

        with (
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.1, **status_kw)),
            patch("genesis.guardian.snapshots._run_subprocess", run),
        ):
            assert await manager.safe_to_snapshot() is False
        assert manager.last_gate_measured is False
        assert not any(c and c[0] == "df" for c in calls)  # never the host filesystem

    @pytest.mark.asyncio
    async def test_undated_lifeline_is_ordered_by_its_name(self, manager) -> None:
        """incus omitted created_at on the NEWEST healthy snapshot: it must not
        sort behind an older dated one (and so lose the rollback target)."""
        older, newer = "guardian-20260101-000000-healthy", "guardian-20260105-000000-healthy"

        async def run(*a, **k):
            return 0, json.dumps([
                {"name": older, "created_at": "2026-01-01T00:00:00Z"},
                {"name": newer},
            ]), ""

        with patch("genesis.guardian.snapshots._run_subprocess", run):
            assert await manager.get_latest_healthy() == newer



@pytest.mark.parametrize("bad", ["3", 60, float("nan")])
@pytest.mark.asyncio
async def test_gate_ignores_an_invalid_reserve(config, bad) -> None:
    """Audit: the gate runs before a recovery action; an invalid reserve must
    neither crash it nor pose as a measured pool refusal."""
    config.storage_pool.min_reserve_pct = bad
    manager = SnapshotManager(config)
    with patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.45)):
        assert await manager.safe_to_snapshot() is True
    assert manager.last_gate_measured is False



class TestRollbackChokepoint:
    """Review round 3 (owner-chosen shape): ONE chokepoint deletes rollback
    snapshots; nothing else can."""

    H = "guardian-20260101-000000-healthy"

    @pytest.mark.asyncio
    async def test_plain_delete_refuses_a_healthy_snapshot(self, manager) -> None:
        fake = _FakeIncus({self.H: _ago(50)})
        with patch("genesis.guardian.snapshots._run_subprocess", fake.run):
            assert await manager.delete(self.H) is False
        assert fake.deleted == []

    @pytest.mark.asyncio
    async def test_chokepoint_needs_confirmation_or_a_replacement(self, manager) -> None:
        fake = _FakeIncus({self.H: _ago(50)})
        with patch("genesis.guardian.snapshots._run_subprocess", fake.run):
            assert await manager.delete_healthy(self.H) is False
            assert fake.deleted == []
            assert await manager.delete_healthy(self.H, replaced_by="guardian-x-healthy") is True
        assert fake.deleted == [self.H]

    @pytest.mark.asyncio
    async def test_unconfirmed_health_never_deletes_first(self, manager) -> None:
        fake = _FakeIncus(
            {"guardian-20260101-000000-healthy": _ago(30)}, create_err=_LVM_THRESHOLD_ERR,
        )
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _with_thin_lvs(fake, manager, live_frac=0.80),
            ),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
        ):
            assert await manager.mark_healthy(delete_first_allowed=True) is None
        assert fake.deleted == []

    @pytest.mark.asyncio
    async def test_prune_never_deletes_a_superseded_healthy(self, config) -> None:
        """Devin: prune removed the fallback right before a refused refresh."""
        config.snapshots.retention = 1
        config.snapshots.max_age_days = 1
        manager = SnapshotManager(config)
        older, newer = "guardian-20260101-000000-healthy", "guardian-20260102-000000-healthy"
        pre = "guardian-20260103-000000-pre-recovery"
        fake = _FakeIncus({older: _ago(90), newer: _ago(60), pre: _ago(50)})
        with patch("genesis.guardian.snapshots._run_subprocess", fake.run):
            await manager.prune()
        assert older not in fake.deleted and newer not in fake.deleted
        assert pre in fake.deleted  # non-healthy age rule still applies

    @pytest.mark.asyncio
    async def test_retention_eviction_defers_delete_first(self, config) -> None:
        """Devin: take() evicted a snapshot, the create was refused, and
        delete-first then took the lifeline before the first delete's space
        came back."""
        config.snapshots.retention = 1
        manager = SnapshotManager(config)
        life = "guardian-20260101-000000-healthy"
        pre = "guardian-20260102-000000-pre-recovery"
        fake = _FakeIncus({life: _ago(30), pre: _ago(29)}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _with_thin_lvs(fake, manager, live_frac=0.80),
            ),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
        ):
            assert await manager.mark_healthy(
                delete_first_allowed=True, healthy_confirmed=True,
            ) is None
        assert fake.deleted == [pre]  # the eviction, and nothing on top of it
        assert life in fake.snaps

    @pytest.mark.parametrize("bad", [-1, "85", 0])
    @pytest.mark.asyncio
    async def test_malformed_tier_is_a_probe_refusal(self, config, bad) -> None:
        config.storage_pool.data_high_pct = bad
        manager = SnapshotManager(config)
        with patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.3)):
            assert await manager.safe_to_snapshot() is False
        assert manager.last_gate_measured is False

    @pytest.mark.parametrize("prefix", ["", "   ", None])
    @pytest.mark.asyncio
    async def test_no_ownership_prefix_creates_nothing(self, config, prefix) -> None:
        config.snapshots.prefix = prefix
        manager = SnapshotManager(config)
        run = AsyncMock()
        with (
            patch.object(manager, "safe_to_snapshot", return_value=True),
            patch("genesis.guardian.snapshots._run_subprocess", run),
        ):
            assert await manager.take(label="healthy") is None
        run.assert_not_called()


@pytest.mark.asyncio
async def test_timed_out_eviction_also_defers_delete_first(config) -> None:
    """Review: a retention delete that TIMED OUT may still be running in the
    daemon, so it must hold delete-first off exactly like a completed one."""
    config.snapshots.retention = 1
    manager = SnapshotManager(config)
    life = "guardian-20260101-000000-healthy"
    pre = "guardian-20260102-000000-pre-recovery"
    fake = _FakeIncus({life: _ago(30), pre: _ago(29)}, create_err=_LVM_THRESHOLD_ERR, until_delete=True)
    inner = _with_thin_lvs(fake, manager, live_frac=0.80)

    async def run(*args, **kwargs):
        if args[:3] == ("incus", "snapshot", "delete") and args[4] == pre:
            return -1, "", "timeout"  # client gave up; the snapshot is still listed
        return await inner(*args, **kwargs)

    with (
        patch.object(manager, "safe_to_snapshot", return_value=True),
        patch("genesis.guardian.snapshots._run_subprocess", run),
        patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_lvm(0.85)),
    ):
        assert await manager.mark_healthy(delete_first_allowed=True, healthy_confirmed=True) is None
    assert manager.last_take_evicted is True
    assert life in fake.snaps and life not in fake.deleted
