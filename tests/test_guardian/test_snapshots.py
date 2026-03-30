"""Tests for Guardian snapshot manager."""

from __future__ import annotations

import json
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
    async def mock(*args, **kwargs):
        return (rc, stdout, stderr)
    return mock


class TestSnapshotTake:

    @pytest.mark.asyncio
    async def test_take_success(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(0, ""),
        ):
            name = await manager.take(label="test")
        assert name is not None
        assert name.startswith("guardian-")
        assert name.endswith("-test")

    @pytest.mark.asyncio
    async def test_take_failure(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(1, "", "error"),
        ):
            name = await manager.take()
        assert name is None

    @pytest.mark.asyncio
    async def test_take_no_label(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(0, ""),
        ):
            name = await manager.take()
        assert name is not None
        assert "-test" not in name


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


class TestSnapshotPrune:

    @pytest.mark.asyncio
    async def test_prune_within_retention(self, manager: SnapshotManager) -> None:
        """Should not prune when within retention limit."""
        with patch.object(
            manager, "list_snapshots",
            return_value=["guardian-1", "guardian-2"],
        ):
            deleted = await manager.prune()
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_prune_over_retention(self, manager: SnapshotManager) -> None:
        """Should prune oldest snapshots past retention (5)."""
        snapshots = [f"guardian-{i}" for i in range(7, 0, -1)]
        with (
            patch.object(manager, "list_snapshots", return_value=snapshots),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _mock_subprocess(0, ""),
            ),
        ):
            deleted = await manager.prune()
        assert deleted == 2  # 7 - 5 = 2

    @pytest.mark.asyncio
    async def test_prune_preserves_healthy(self, manager: SnapshotManager) -> None:
        """The most recent healthy snapshot should be preserved even if old."""
        snapshots = [
            "guardian-6", "guardian-5", "guardian-4", "guardian-3",
            "guardian-2", "guardian-1-healthy",  # oldest but healthy
        ]
        with (
            patch.object(manager, "list_snapshots", return_value=snapshots),
            patch(
                "genesis.guardian.snapshots._run_subprocess",
                _mock_subprocess(0, ""),
            ),
        ):
            await manager.prune()
        # guardian-1-healthy should NOT be deleted even though it's outside retention


class TestMarkHealthy:

    @pytest.mark.asyncio
    async def test_mark_healthy(self, manager: SnapshotManager) -> None:
        with patch(
            "genesis.guardian.snapshots._run_subprocess",
            _mock_subprocess(0, ""),
        ):
            name = await manager.mark_healthy()
        assert name is not None
        assert name.endswith("-healthy")


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
