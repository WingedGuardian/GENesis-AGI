"""Tests for GenesisVersionCollector — self-update detection."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.learning.signals.genesis_version import GenesisVersionCollector


@pytest.fixture()
async def db(tmp_path):
    """In-memory SQLite with observations table."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "CREATE TABLE observations ("
            "  id TEXT PRIMARY KEY,"
            "  person_id TEXT,"
            "  source TEXT NOT NULL, type TEXT NOT NULL, category TEXT,"
            "  content TEXT NOT NULL,"
            "  priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),"
            "  speculative INTEGER NOT NULL DEFAULT 0,"
            "  retrieved_count INTEGER NOT NULL DEFAULT 0,"
            "  influenced_action INTEGER NOT NULL DEFAULT 0,"
            "  resolved INTEGER NOT NULL DEFAULT 0,"
            "  resolved_at TEXT, resolution_notes TEXT,"
            "  created_at TEXT NOT NULL, expires_at TEXT, content_hash TEXT"
            ")"
        )
        await conn.commit()
        yield conn


@pytest.fixture()
def collector(db):
    return GenesisVersionCollector(db)


def _mock_head(version: str):
    return patch.object(
        GenesisVersionCollector, "_get_head", new_callable=AsyncMock, return_value=version,
    )


def _mock_upstream(behind: int, summary: str = ""):
    return patch.object(
        GenesisVersionCollector, "_check_upstream",
        new_callable=AsyncMock, return_value=(behind, summary),
    )


def _mock_failure_file_check():
    return patch.object(
        GenesisVersionCollector, "_check_failure_file", new_callable=AsyncMock,
    )


def _mock_store_update_available(returned_value: bool = True):
    return patch.object(
        GenesisVersionCollector, "_store_update_available",
        new_callable=AsyncMock, return_value=returned_value,
    )


class TestFirstRunBehavior:
    """Baseline storage and signal=0 on first observation."""

    @pytest.mark.asyncio
    async def test_first_run_stores_baseline(self, collector, db) -> None:
        with _mock_head("abc123"), _mock_failure_file_check():
            reading = await collector.collect()

        assert reading.value == 0.0
        assert reading.name == "genesis_version_changed"

        cursor = await db.execute(
            "SELECT content FROM observations "
            "WHERE source = 'genesis_version' AND type = 'genesis_version_baseline'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert json.loads(row["content"])["version"] == "abc123"


class TestLocalVersionChange:
    """Local HEAD change detection + observation resolution."""

    @pytest.mark.asyncio
    async def test_local_change_emits_signal_and_resolves_pending(
        self, collector, db,
    ) -> None:
        # Seed baseline + a pending update_available observation
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a', 'genesis_version', 'genesis_version_baseline', "
            "?, 'low', '2026-04-01T00:00:00Z')",
            (json.dumps({"version": "old123"}),),
        )
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('b', 'genesis_version', 'genesis_update_available', "
            "?, 'medium', '2026-04-01T00:00:00Z')",
            (json.dumps({
                "current_commit": "old123",
                "target_commit": "new456",
                "commits_behind": 3,
            }),),
        )
        await db.commit()

        with _mock_head("new456"), _mock_failure_file_check():
            reading = await collector.collect()

        assert reading.value == 1.0

        # Pending update_available should be resolved
        cursor = await db.execute(
            "SELECT resolved, resolution_notes FROM observations WHERE id = 'b'",
        )
        row = await cursor.fetchone()
        assert row["resolved"] == 1
        assert "new456" in row["resolution_notes"]

        # Baseline should be updated
        cursor = await db.execute(
            "SELECT content FROM observations "
            "WHERE source = 'genesis_version' AND type = 'genesis_version_baseline' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert json.loads(row["content"])["version"] == "new456"

        # Change observation should exist
        cursor = await db.execute(
            "SELECT content FROM observations "
            "WHERE source = 'genesis_version' AND type = 'genesis_version_change'"
        )
        row = await cursor.fetchone()
        data = json.loads(row["content"])
        assert data["old_version"] == "old123"
        assert data["new_version"] == "new456"


class TestUpstreamCheck:
    """Self-throttled upstream fetch + dedup."""

    @pytest.mark.asyncio
    async def test_upstream_behind_stores_and_signals(self, collector, db) -> None:
        # Seed baseline so we go to upstream check
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a', 'genesis_version', 'genesis_version_baseline', "
            "?, 'low', '2026-04-01T00:00:00Z')",
            (json.dumps({"version": "abc123"}),),
        )
        await db.commit()

        with _mock_head("abc123"), _mock_failure_file_check(), \
             _mock_upstream(3, "feat: new thing\nfix: old thing"), \
             _mock_store_update_available(True):
            reading = await collector.collect()

        assert reading.value == 1.0

    @pytest.mark.asyncio
    async def test_upstream_failure_logs_error_not_silent(
        self, collector, db, caplog,
    ) -> None:
        """git fetch failure should be logged at ERROR, not hidden."""
        import logging

        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a', 'genesis_version', 'genesis_version_baseline', "
            "?, 'low', '2026-04-01T00:00:00Z')",
            (json.dumps({"version": "abc123"}),),
        )
        await db.commit()

        async def raising_upstream(self):
            raise RuntimeError("git fetch failed: network down")

        with _mock_head("abc123"), _mock_failure_file_check(), \
             patch.object(GenesisVersionCollector, "_check_upstream", raising_upstream), \
             caplog.at_level(logging.ERROR):
            reading = await collector.collect()

        assert reading.value == 0.0
        # Verify error was logged at ERROR level with the actual reason
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("Upstream check failed" in r.message for r in error_records), \
            f"Expected ERROR log, got: {[r.message for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_throttle_skips_fetch_within_interval(self, collector, db) -> None:
        """After a fetch, subsequent collect() calls within interval skip."""
        from datetime import UTC, datetime

        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a', 'genesis_version', 'genesis_version_baseline', "
            "?, 'low', '2026-04-01T00:00:00Z')",
            (json.dumps({"version": "abc123"}),),
        )
        await db.commit()

        # Pretend a fetch just happened
        collector._last_fetch_at = datetime.now(UTC)

        upstream_mock = AsyncMock(return_value=(0, ""))
        with _mock_head("abc123"), _mock_failure_file_check(), \
             patch.object(GenesisVersionCollector, "_check_upstream", upstream_mock):
            await collector.collect()

        upstream_mock.assert_not_called()


class TestUpdateAvailableDedup:
    """Same target commit only stores one observation."""

    @pytest.mark.asyncio
    async def test_dedup_by_target_commit(self, collector, db) -> None:
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a', 'genesis_version', 'genesis_version_baseline', "
            "?, 'low', '2026-04-01T00:00:00Z')",
            (json.dumps({"version": "abc123"}),),
        )
        # Pre-existing unresolved observation for the same target
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('b', 'genesis_version', 'genesis_update_available', "
            "?, 'medium', '2026-04-01T00:00:00Z')",
            (json.dumps({
                "current_commit": "abc123",
                "target_commit": "def456",
                "commits_behind": 2,
            }),),
        )
        await db.commit()

        async def fake_subprocess_target(*args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.communicate = AsyncMock(return_value=(b"def456", b""))
            return mock

        with patch(
            "genesis.learning.signals.genesis_version.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess_target,
        ):
            stored = await collector._store_update_available("abc123", 2, "summary")

        assert stored is False  # Dedup skipped

        # Still only one observation
        cursor = await db.execute(
            "SELECT COUNT(*) AS cnt FROM observations "
            "WHERE type = 'genesis_update_available'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 1


class TestFailureArchive:
    """M3 + M4: archive lifecycle for ~/.genesis/last_update_failure.json."""

    @pytest.fixture(autouse=True)
    def _patch_archive_paths(self, tmp_path, monkeypatch):
        """Redirect archive constants to tmp_path so tests are isolated."""
        fake_failure = tmp_path / "last_update_failure.json"
        fake_archive_dir = tmp_path / "update-failures"
        monkeypatch.setattr(
            "genesis.learning.signals.genesis_version._FAILURE_FILE",
            fake_failure,
        )
        monkeypatch.setattr(
            "genesis.learning.signals.genesis_version._FAILURE_ARCHIVE_DIR",
            fake_archive_dir,
        )
        self.fake_failure = fake_failure
        self.fake_archive_dir = fake_archive_dir

    def test_archive_creates_timestamped_file(self) -> None:
        self.fake_failure.write_text('{"timestamp": "2026-04-10T12:00:00Z"}')
        GenesisVersionCollector._archive_failure_file()

        assert not self.fake_failure.exists(), "live file should be moved"
        assert self.fake_archive_dir.is_dir()
        archives = list(self.fake_archive_dir.iterdir())
        assert len(archives) == 1
        name = archives[0].name
        assert name.startswith("last_update_failure.")
        assert name.endswith(".json")
        # Contains a timestamp token (YYYYMMDDTHHMMSSZ)
        assert any(c.isdigit() for c in name)

    def test_repeated_failures_do_not_overwrite(self, monkeypatch) -> None:
        """M3 regression: same-second failures produce distinct archives.

        Freezes time for ALL three archive calls so the filename shape
        is deterministic: the first lands at `...20260410T120000Z.json`,
        then collision fallback produces `.1.json` and `.2.json`.
        """
        from datetime import UTC, datetime

        fixed = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(
            "genesis.learning.signals.genesis_version.datetime", _FrozenDT,
        )

        self.fake_failure.write_text('{"timestamp": "first"}')
        GenesisVersionCollector._archive_failure_file()

        self.fake_failure.write_text('{"timestamp": "second"}')
        GenesisVersionCollector._archive_failure_file()

        self.fake_failure.write_text('{"timestamp": "third"}')
        GenesisVersionCollector._archive_failure_file()

        archive_names = {p.name for p in self.fake_archive_dir.iterdir()}
        assert archive_names == {
            "last_update_failure.20260410T120000Z.json",
            "last_update_failure.20260410T120000Z.1.json",
            "last_update_failure.20260410T120000Z.2.json",
        }, f"unexpected archive names: {archive_names}"

    def test_prune_caps_archive_at_limit(self, monkeypatch) -> None:
        """M4 regression: archive directory must not grow unbounded."""
        import time

        monkeypatch.setattr(
            "genesis.learning.signals.genesis_version._FAILURE_ARCHIVE_CAP", 3,
        )
        self.fake_archive_dir.mkdir(parents=True, exist_ok=True)

        # Create 5 fake archive files with distinct mtimes (oldest first)
        now = time.time()
        for i in range(5):
            p = self.fake_archive_dir / f"last_update_failure.stub{i}.json"
            p.write_text("{}")
            import os
            os.utime(p, (now - (5 - i) * 10, now - (5 - i) * 10))

        GenesisVersionCollector._prune_failure_archive()

        remaining = sorted(self.fake_archive_dir.iterdir())
        assert len(remaining) == 3, (
            f"expected 3 after prune, got {len(remaining)}"
        )
        # The three newest (stub2, stub3, stub4) must survive
        names = {p.name for p in remaining}
        assert "last_update_failure.stub4.json" in names
        assert "last_update_failure.stub3.json" in names
        assert "last_update_failure.stub2.json" in names
        assert "last_update_failure.stub0.json" not in names
        assert "last_update_failure.stub1.json" not in names

    def test_prune_handles_missing_dir(self) -> None:
        """Prune must be safe to call when archive dir doesn't exist yet."""
        assert not self.fake_archive_dir.exists()
        # Should not raise
        GenesisVersionCollector._prune_failure_archive()

    def test_archive_noop_when_no_live_file(self) -> None:
        """_archive_failure_file must be a no-op if the live file is absent."""
        assert not self.fake_failure.exists()
        GenesisVersionCollector._archive_failure_file()
        # No archive created
        assert not self.fake_archive_dir.exists() or not any(
            self.fake_archive_dir.iterdir()
        )

    def test_prune_preserves_foreign_files(self) -> None:
        """M2 regression: prune must NOT delete files it doesn't own.

        The archive directory may one day host other diagnostic files.
        The prune filter must match ``last_update_failure.*.json``
        exactly, not just the ``last_update_failure.`` prefix.
        """
        import os
        import time

        self.fake_archive_dir.mkdir(parents=True, exist_ok=True)

        # Files the prune MUST NOT touch
        foreign_files = [
            self.fake_archive_dir / "last_update_failure.notes.txt",
            self.fake_archive_dir / "diagnostics.json",
            self.fake_archive_dir / "README.md",
            self.fake_archive_dir / "last_update_failure_unrelated",
        ]
        for f in foreign_files:
            f.write_text("preserve me")

        # Fill the archive past the cap with real archive entries.
        now = time.time()
        for i in range(15):
            p = self.fake_archive_dir / f"last_update_failure.stub{i:02d}.json"
            p.write_text("{}")
            os.utime(p, (now - (15 - i) * 10, now - (15 - i) * 10))

        GenesisVersionCollector._prune_failure_archive()

        # All 4 foreign files untouched
        for f in foreign_files:
            assert f.exists(), f"prune deleted foreign file: {f.name}"

        # Archive bounded to cap (10)
        archives = [
            p for p in self.fake_archive_dir.iterdir()
            if p.is_file()
            and p.suffix == ".json"
            and p.name.startswith("last_update_failure.")
        ]
        assert len(archives) == 10

    def test_prune_ignores_non_json_with_archive_prefix(self) -> None:
        """Files like `last_update_failure.old` (no .json) must be ignored."""
        self.fake_archive_dir.mkdir(parents=True, exist_ok=True)

        tempfile = self.fake_archive_dir / "last_update_failure.old"
        tempfile.write_text("legacy")

        # Fill past cap with valid entries
        for i in range(15):
            p = self.fake_archive_dir / f"last_update_failure.stub{i:02d}.json"
            p.write_text("{}")

        GenesisVersionCollector._prune_failure_archive()

        assert tempfile.exists(), "non-.json file was wrongly pruned"

    def test_archive_collision_fallback_is_bounded(self, monkeypatch) -> None:
        """L1 regression: the collision fallback loop has a hard cap.

        Simulates a wedged filesystem where every candidate filename
        already exists, and verifies that _archive_failure_file()
        returns cleanly rather than spinning forever.
        """
        from datetime import UTC, datetime

        fixed = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(
            "genesis.learning.signals.genesis_version.datetime", _FrozenDT,
        )

        # Patch Path.exists() on the specific archive directory so every
        # candidate in the collision loop appears occupied.
        self.fake_archive_dir.mkdir(parents=True, exist_ok=True)
        self.fake_failure.write_text('{"ts": "stuck"}')

        real_exists = type(self.fake_archive_dir).exists
        archive_dir_str = str(self.fake_archive_dir)

        def always_exists_for_candidates(self_path):
            if str(self_path).startswith(archive_dir_str + "/last_update_failure."):
                return True
            return real_exists(self_path)

        monkeypatch.setattr(
            type(self.fake_archive_dir), "exists", always_exists_for_candidates,
        )

        # Must return without raising or hanging. The live file should
        # still be present since archival didn't succeed.
        GenesisVersionCollector._archive_failure_file()
        assert self.fake_failure.exists()


class TestCheckDisabled:
    """check.enabled=false short-circuits the collector."""

    @pytest.mark.asyncio
    async def test_disabled_returns_zero_no_db_writes(self, collector, db) -> None:
        with patch(
            "genesis.learning.signals.genesis_version._load_updates_config",
            return_value={"check": {"enabled": False}},
        ):
            reading = await collector.collect()

        assert reading.value == 0.0

        # No baseline created when disabled
        cursor = await db.execute(
            "SELECT COUNT(*) AS cnt FROM observations WHERE source = 'genesis_version'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 0
