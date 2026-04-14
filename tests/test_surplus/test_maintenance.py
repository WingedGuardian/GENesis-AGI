"""Tests for infrastructure maintenance surplus executors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.surplus.maintenance import (
    BackupVerificationExecutor,
    DbMaintenanceExecutor,
    DeadLetterReplayExecutor,
    DiskCleanupExecutor,
    _fmt_bytes,
)
from genesis.surplus.types import ComputeTier, SurplusTask, TaskStatus, TaskType


def _make_task(task_type: TaskType) -> SurplusTask:
    return SurplusTask(
        id="test-task-001",
        task_type=task_type,
        compute_tier=ComputeTier.FREE_API,
        priority=0.5,
        drive_alignment="preservation",
        status=TaskStatus.RUNNING,
        created_at=datetime.now(UTC).isoformat(),
    )


# ── DiskCleanupExecutor ─────────────────────────────────────────────────

class TestDiskCleanup:
    @pytest.mark.asyncio
    async def test_empty_dirs(self, tmp_path, monkeypatch):
        """When allowlisted dirs don't exist, reports nothing to clean."""
        import genesis.surplus.maintenance as maint
        # Patch _CLEANUP_RULES to use tmp_path
        monkeypatch.setattr(maint, "_CLEANUP_RULES", [
            {
                "description": "Test logs",
                "base": tmp_path / "nonexistent",
                "pattern": "*.log",
                "max_age_days": 7,
                "action": "report",
            },
        ])
        executor = DiskCleanupExecutor()
        result = await executor.execute(_make_task(TaskType.DISK_CLEANUP))
        assert result.success is True
        assert "Nothing to clean" in result.content

    @pytest.mark.asyncio
    async def test_finds_old_files(self, tmp_path, monkeypatch):
        """Detects files older than max_age_days."""
        import genesis.surplus.maintenance as maint
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        # Create an old file
        old_file = log_dir / "old.log"
        old_file.write_text("old log content")
        import os
        old_mtime = (datetime.now(UTC) - timedelta(days=10)).timestamp()
        os.utime(old_file, (old_mtime, old_mtime))

        # Create a recent file
        new_file = log_dir / "new.log"
        new_file.write_text("recent")

        monkeypatch.setattr(maint, "_CLEANUP_RULES", [
            {
                "description": "Test logs",
                "base": log_dir,
                "pattern": "*.log",
                "max_age_days": 7,
                "action": "report",
            },
        ])
        executor = DiskCleanupExecutor()
        result = await executor.execute(_make_task(TaskType.DISK_CLEANUP))
        assert result.success is True
        assert "1 files" in result.content
        assert result.insights[0]["reclaimable_files"] == 1


# ── BackupVerificationExecutor ───────────────────────────────────────────

class TestBackupVerification:
    @pytest.mark.asyncio
    async def test_no_backup_dir(self, tmp_path, monkeypatch):
        """Reports stale when backup dir doesn't exist."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        executor = BackupVerificationExecutor()
        result = await executor.execute(_make_task(TaskType.BACKUP_VERIFICATION))
        assert result.success is True
        assert result.insights[0]["backup_stale"] is True

    @pytest.mark.asyncio
    async def test_fresh_marker(self, tmp_path, monkeypatch):
        """Reports OK when backup marker is recent."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        backup_dir = tmp_path / ".genesis" / "backups"
        backup_dir.mkdir(parents=True)
        marker = backup_dir / "last_backup_at"
        marker.write_text(datetime.now(UTC).isoformat())

        executor = BackupVerificationExecutor()
        result = await executor.execute(_make_task(TaskType.BACKUP_VERIFICATION))
        assert result.success is True
        assert result.insights[0]["backup_stale"] is False
        assert "OK" in result.content

    @pytest.mark.asyncio
    async def test_stale_marker(self, tmp_path, monkeypatch):
        """Reports stale when backup is older than threshold."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        backup_dir = tmp_path / ".genesis" / "backups"
        backup_dir.mkdir(parents=True)
        marker = backup_dir / "last_backup_at"
        old_time = datetime.now(UTC) - timedelta(hours=48)
        marker.write_text(old_time.isoformat())

        executor = BackupVerificationExecutor(max_age_hours=24)
        result = await executor.execute(_make_task(TaskType.BACKUP_VERIFICATION))
        assert result.success is True
        assert result.insights[0]["backup_stale"] is True
        assert "WARNING" in result.content


# ── DeadLetterReplayExecutor ─────────────────────────────────────────────

class TestDeadLetterReplay:
    @pytest.mark.asyncio
    async def test_empty_queue(self):
        """Reports empty when no pending items."""
        dlq = AsyncMock()
        dlq.get_pending_count = AsyncMock(return_value=0)
        router = MagicMock()

        executor = DeadLetterReplayExecutor(dead_letter=dlq, router=router)
        result = await executor.execute(_make_task(TaskType.DEAD_LETTER_REPLAY))
        assert result.success is True
        assert "empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_replays_pending(self):
        """Replays pending items and reports results."""
        dlq = AsyncMock()
        dlq.get_pending_count = AsyncMock(side_effect=[3, 1])
        dlq.expire_old = AsyncMock(return_value=1)
        dlq.redispatch = AsyncMock(return_value=(1, 1))
        router = MagicMock()

        executor = DeadLetterReplayExecutor(dead_letter=dlq, router=router)
        result = await executor.execute(_make_task(TaskType.DEAD_LETTER_REPLAY))
        assert result.success is True
        assert "1 succeeded" in result.content
        assert "1 failed" in result.content
        assert result.insights[0]["dlq_succeeded"] == 1


# ── DbMaintenanceExecutor ───────────────────────────────────────────────

class TestDbMaintenance:
    @pytest.mark.asyncio
    async def test_reports_stats(self, tmp_path):
        """Generates a DB health report."""
        import aiosqlite

        db_path = tmp_path / "test.db"
        async with aiosqlite.connect(str(db_path)) as db:
            # Create a table so there's something to report
            await db.execute("CREATE TABLE observations (id TEXT PRIMARY KEY)")
            await db.execute("INSERT INTO observations VALUES ('test1')")
            await db.commit()

            # Monkey-patch genesis_db_path to return our test db
            import genesis.env
            orig = genesis.env.genesis_db_path

            def _test_db_path():
                return db_path

            genesis.env.genesis_db_path = _test_db_path
            try:
                executor = DbMaintenanceExecutor(db=db)
                result = await executor.execute(_make_task(TaskType.DB_MAINTENANCE))
            finally:
                genesis.env.genesis_db_path = orig

        assert result.success is True
        assert "observations: 1" in result.content
        assert "Integrity: ok" in result.content


# ── Helpers ──────────────────────────────────────────────────────────────

class TestFmtBytes:
    def test_bytes(self):
        assert _fmt_bytes(500) == "500 B"

    def test_kilobytes(self):
        assert "KB" in _fmt_bytes(2048)

    def test_megabytes(self):
        assert "MB" in _fmt_bytes(5 * 1024 * 1024)

    def test_gigabytes(self):
        assert "GB" in _fmt_bytes(2 * 1024 * 1024 * 1024)
