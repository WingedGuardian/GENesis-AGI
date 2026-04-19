"""Tests for CC version signal collector."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.learning.signals.cc_version import CCVersionCollector


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
    return CCVersionCollector(db)


@pytest.fixture()
def collector_with_pipeline(db):
    mock_pipeline = MagicMock()
    return CCVersionCollector(db, pipeline_getter=lambda: mock_pipeline), mock_pipeline


def _mock_version(version: str):
    """Patch _get_cc_version to return a fixed string."""
    return patch.object(
        CCVersionCollector, "_get_cc_version", new_callable=AsyncMock, return_value=version,
    )


class TestVersionDetection:
    """CC version change detection."""

    @pytest.mark.asyncio
    async def test_first_run_stores_and_returns_zero(self, collector, db) -> None:
        """First run (no stored version) stores current, returns 0.0."""
        with _mock_version("1.0.0"):
            reading = await collector.collect()

        assert reading.value == 0.0
        assert reading.name == "cc_version_changed"

        # Check version was stored in observations
        cursor = await db.execute(
            "SELECT content FROM observations "
            "WHERE source = 'cc_version' AND type = 'cc_version_baseline'"
        )
        row = await cursor.fetchone()
        assert row is not None
        data = json.loads(row["content"])
        assert data["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_no_change_returns_zero(self, collector, db) -> None:
        """Same version returns 0.0."""
        with _mock_version("1.0.0"):
            await collector.collect()  # First run — stores
            reading = await collector.collect()  # Second run — no change

        assert reading.value == 0.0

    @pytest.mark.asyncio
    async def test_version_change_detected(self, collector, db) -> None:
        """Version change emits 1.0 and stores observation."""
        with _mock_version("1.0.0"):
            await collector.collect()  # Store initial

        with _mock_version("1.1.0"):
            reading = await collector.collect()

        assert reading.value == 1.0
        assert reading.name == "cc_version_changed"

        # Check change observation stored
        cursor = await db.execute(
            "SELECT content FROM observations WHERE type = 'version_change'"
        )
        row = await cursor.fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data["old_version"] == "1.0.0"
        assert data["new_version"] == "1.1.0"

    @pytest.mark.asyncio
    async def test_subprocess_failure_returns_failed(self, collector) -> None:
        """When claude --version fails, returns failed reading."""
        with patch.object(
            CCVersionCollector, "_get_cc_version",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError("claude not found"),
        ):
            reading = await collector.collect()

        assert reading.value == 0.0
        assert reading.failed is True

    @pytest.mark.asyncio
    async def test_recovery_after_failure(self, collector, db) -> None:
        """After a subprocess failure, next successful collect works normally."""
        # First: succeed
        with _mock_version("1.0.0"):
            await collector.collect()

        # Second: fail
        with patch.object(
            CCVersionCollector, "_get_cc_version",
            new_callable=AsyncMock,
            side_effect=OSError("subprocess error"),
        ):
            reading = await collector.collect()
        assert reading.failed is True

        # Third: succeed, same version — should return 0.0 (no change)
        with _mock_version("1.0.0"):
            reading = await collector.collect()
        assert reading.value == 0.0
        assert reading.failed is False


class TestAnalyzerIntegration:
    """Verify CCUpdateAnalyzer is called on version change."""

    @pytest.mark.asyncio
    async def test_analyzer_called_on_version_change(self, db) -> None:
        """When router is provided and version changes, analyzer runs."""
        router = MagicMock()
        collector = CCVersionCollector(db, router=router)

        mock_analyze = AsyncMock(return_value={"impact": "informational", "finding_id": "f-1"})

        with _mock_version("1.0.0"):
            await collector.collect()  # Store initial

        with (
            _mock_version("1.1.0"),
            patch("genesis.recon.cc_update_analyzer.CCUpdateAnalyzer") as MockAnalyzer,
        ):
            MockAnalyzer.return_value.analyze = mock_analyze
            reading = await collector.collect()

        assert reading.value == 1.0
        MockAnalyzer.assert_called_once_with(db=db, router=router, pipeline=None, memory_store=None)
        mock_analyze.assert_awaited_once_with("1.0.0", "1.1.0")

    @pytest.mark.asyncio
    async def test_analyzer_not_called_without_version_change(self, db) -> None:
        """No version change means no analyzer call."""
        router = MagicMock()
        collector = CCVersionCollector(db, router=router)

        with (
            _mock_version("1.0.0"),
            patch("genesis.recon.cc_update_analyzer.CCUpdateAnalyzer") as MockAnalyzer,
        ):
            await collector.collect()  # First run
            await collector.collect()  # Same version

        MockAnalyzer.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyzer_timeout_still_signals(self, db) -> None:
        """When analyzer times out, signal still fires with value=1.0."""
        router = MagicMock()
        collector = CCVersionCollector(db, router=router)

        with _mock_version("1.0.0"):
            await collector.collect()

        async def slow_analyze(*args, **kwargs):
            await asyncio.sleep(999)

        with (
            _mock_version("1.1.0"),
            patch("genesis.recon.cc_update_analyzer.CCUpdateAnalyzer") as MockAnalyzer,
            patch("genesis.learning.signals.cc_version.asyncio.wait_for", side_effect=TimeoutError),
        ):
            MockAnalyzer.return_value.analyze = AsyncMock(side_effect=slow_analyze)
            reading = await collector.collect()

        assert reading.value == 1.0
        assert reading.failed is False

    @pytest.mark.asyncio
    async def test_analyzer_exception_still_signals(self, db) -> None:
        """When analyzer raises, signal still fires with value=1.0."""
        router = MagicMock()
        collector = CCVersionCollector(db, router=router)

        with _mock_version("1.0.0"):
            await collector.collect()

        with (
            _mock_version("1.1.0"),
            patch("genesis.recon.cc_update_analyzer.CCUpdateAnalyzer") as MockAnalyzer,
        ):
            MockAnalyzer.return_value.analyze = AsyncMock(side_effect=RuntimeError("boom"))
            reading = await collector.collect()

        assert reading.value == 1.0
        assert reading.failed is False

    @pytest.mark.asyncio
    async def test_no_router_analyzer_runs_without_llm(self, db) -> None:
        """Without router, analyzer runs but falls back to non-LLM path."""
        collector = CCVersionCollector(db)  # No router

        mock_analyze = AsyncMock(return_value={"impact": "informational", "finding_id": "f-2"})

        with _mock_version("1.0.0"):
            await collector.collect()

        with (
            _mock_version("1.1.0"),
            patch("genesis.recon.cc_update_analyzer.CCUpdateAnalyzer") as MockAnalyzer,
        ):
            MockAnalyzer.return_value.analyze = mock_analyze
            reading = await collector.collect()

        assert reading.value == 1.0
        # Analyzer is called with router=None — it works without LLM, just stores basic finding
        MockAnalyzer.assert_called_once_with(db=db, router=None, pipeline=None, memory_store=None)
        mock_analyze.assert_awaited_once_with("1.0.0", "1.1.0")

    @pytest.mark.asyncio
    async def test_registry_check_runs_on_no_change(self, db) -> None:
        """When version is unchanged, registry check fires."""
        collector = CCVersionCollector(db)

        with _mock_version("1.0.0"):
            await collector.collect()  # Store initial

        with (
            _mock_version("1.0.0"),
            patch.object(
                CCVersionCollector, "_check_registry_version", new_callable=AsyncMock,
            ) as mock_check,
        ):
            reading = await collector.collect()

        assert reading.value == 0.0
        mock_check.assert_awaited_once_with("1.0.0")

    @pytest.mark.asyncio
    async def test_registry_check_failure_does_not_break(self, db) -> None:
        """Registry check failure is swallowed — reading still returned."""
        collector = CCVersionCollector(db)

        with _mock_version("1.0.0"):
            await collector.collect()

        with (
            _mock_version("1.0.0"),
            patch.object(
                CCVersionCollector, "_check_registry_version",
                new_callable=AsyncMock, side_effect=RuntimeError("npm broken"),
            ),
        ):
            reading = await collector.collect()

        assert reading.value == 0.0
        assert reading.failed is False

    @pytest.mark.asyncio
    async def test_pipeline_threaded_to_analyzer(self, db) -> None:
        """Pipeline getter is resolved and passed through to CCUpdateAnalyzer."""
        router = MagicMock()
        mock_pipeline = MagicMock()
        collector = CCVersionCollector(db, router=router, pipeline_getter=lambda: mock_pipeline)

        mock_analyze = AsyncMock(return_value={"impact": "informational", "finding_id": "f-3"})

        with _mock_version("1.0.0"):
            await collector.collect()

        with (
            _mock_version("1.1.0"),
            patch("genesis.recon.cc_update_analyzer.CCUpdateAnalyzer") as MockAnalyzer,
        ):
            MockAnalyzer.return_value.analyze = mock_analyze
            await collector.collect()

        MockAnalyzer.assert_called_once_with(db=db, router=router, pipeline=mock_pipeline, memory_store=None)


class TestRegistryCheck:
    """Remote npm registry version monitoring.

    _check_registry_version() is now a no-op — Genesis is pegged to a
    specific CC version. These tests verify it produces no observations.
    """

    @pytest.mark.asyncio
    async def test_registry_check_is_noop(self, collector, db) -> None:
        """_check_registry_version is a no-op — no observations stored."""
        await collector._check_registry_version("1.0.0")

        cursor = await db.execute(
            "SELECT count(*) FROM observations WHERE type = 'cc_version_available'",
        )
        row = await cursor.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_registry_check_silent(self, collector, db) -> None:
        """No-op doesn't raise regardless of input."""
        await collector._check_registry_version("1.0.0")  # Should not raise

        cursor = await db.execute(
            "SELECT count(*) FROM observations WHERE type = 'cc_version_available'",
        )
        row = await cursor.fetchone()
        assert row[0] == 0

    def test_is_newer_basic(self) -> None:
        """Semver comparison works correctly."""
        assert CCVersionCollector._is_newer("2.0.0", "1.0.0") is True
        assert CCVersionCollector._is_newer("1.0.0", "2.0.0") is False
        assert CCVersionCollector._is_newer("1.0.0", "1.0.0") is False
        assert CCVersionCollector._is_newer("2.1.90", "2.1.89") is True
        assert CCVersionCollector._is_newer("2.1.89", "2.1.90") is False

    def test_is_newer_with_suffix(self) -> None:
        """Semver comparison strips non-numeric suffixes."""
        assert CCVersionCollector._is_newer("2.1.90 (Claude Code)", "2.1.89 (Claude Code)") is True
        assert CCVersionCollector._is_newer("2.1.89", "2.1.90 (Claude Code)") is False
