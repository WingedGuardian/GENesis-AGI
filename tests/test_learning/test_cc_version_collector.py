"""Tests for CC version signal collector."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

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
            "  created_at TEXT NOT NULL, expires_at TEXT, content_hash TEXT, origin_class TEXT"
            ")"
        )
        await conn.commit()
        yield conn


@pytest.fixture()
def collector(db):
    return CCVersionCollector(db)


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


class TestNoImpactAnalysis:
    """The collector is DETECT-AND-RECORD ONLY — it must never run analysis.

    Impact analysis was deliberately removed from this collector: driving a
    variable-cost multi-LLM operation from a signal tick was a reliability bug in
    every form (awaited inline, a fixed caller deadline cancelled it and stored NO
    finding on exactly the big multi-release jump it existed for; dispatched as a
    background task, it was lost on shutdown after the baseline had already
    advanced, so it never retried). Analysis now lives on the on-demand
    ``recon_cc_update_check`` MCP path and the idempotent daily pre-eval job.
    """

    @pytest.mark.asyncio
    async def test_version_change_records_without_invoking_analyzer(self, collector, db) -> None:
        """A version change records + signals 1.0 and NEVER touches the analyzer."""
        with _mock_version("1.0.0"):
            await collector.collect()  # establish baseline

        with (
            _mock_version("1.1.0"),
            patch("genesis.recon.cc_update_analyzer.CCUpdateAnalyzer") as MockAnalyzer,
        ):
            reading = await collector.collect()

        assert reading.value == 1.0
        assert reading.failed is False
        # The durable record is written...
        cursor = await db.execute(
            "SELECT content FROM observations WHERE type = 'version_change'"
        )
        row = await cursor.fetchone()
        assert row is not None
        data = json.loads(row["content"])
        assert data["old_version"] == "1.0.0"
        assert data["new_version"] == "1.1.0"
        # ...and the analyzer is never constructed or called.
        MockAnalyzer.assert_not_called()

    def test_collector_exposes_no_analysis_surface(self) -> None:
        """The analysis method and the params that fed it are gone for good.

        Guards the REMOVAL itself: a refactor that reintroduces _analyze_update —
        or the router/pipeline/memory_store wiring that existed only to feed it —
        would resurrect the lifecycle-bug class this removal eliminated.
        """
        import inspect

        assert not hasattr(CCVersionCollector, "_analyze_update")
        params = set(inspect.signature(CCVersionCollector.__init__).parameters)
        assert params == {"self", "db"}

    @pytest.mark.asyncio
    async def test_change_signals_once_then_resets(self, collector) -> None:
        """The baseline advances, so the change is a one-shot event signal."""
        with _mock_version("1.0.0"):
            await collector.collect()
        with _mock_version("1.1.0"):
            first = await collector.collect()
        with (
            _mock_version("1.1.0"),
            patch.object(
                CCVersionCollector, "_check_registry_version", new_callable=AsyncMock,
            ),
        ):
            second = await collector.collect()

        assert first.value == 1.0
        assert second.value == 0.0

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



class TestRegistryCheck:
    """Remote npm registry version monitoring."""

    @pytest.mark.asyncio
    async def test_registry_newer_creates_observation(self, collector, db) -> None:
        """When registry has a newer version, a cc_version_available observation is stored."""
        with patch.object(
            CCVersionCollector, "_get_registry_version",
            new_callable=AsyncMock, return_value="2.0.0",
        ):
            await collector._check_registry_version("1.0.0")

        cursor = await db.execute(
            "SELECT content FROM observations WHERE type = 'cc_version_available'",
        )
        row = await cursor.fetchone()
        assert row is not None
        import json as _json
        data = _json.loads(row[0])
        assert data["installed"] == "1.0.0"
        assert data["available"] == "2.0.0"

    @pytest.mark.asyncio
    async def test_registry_same_version_no_observation(self, collector, db) -> None:
        """When registry matches installed, no observation is created."""
        with patch.object(
            CCVersionCollector, "_get_registry_version",
            new_callable=AsyncMock, return_value="1.0.0",
        ):
            await collector._check_registry_version("1.0.0")

        cursor = await db.execute(
            "SELECT count(*) FROM observations WHERE type = 'cc_version_available'",
        )
        row = await cursor.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_registry_older_version_no_observation(self, collector, db) -> None:
        """When registry is older than installed, no observation is created."""
        with patch.object(
            CCVersionCollector, "_get_registry_version",
            new_callable=AsyncMock, return_value="0.9.0",
        ):
            await collector._check_registry_version("1.0.0")

        cursor = await db.execute(
            "SELECT count(*) FROM observations WHERE type = 'cc_version_available'",
        )
        row = await cursor.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_registry_empty_response_no_observation(self, collector, db) -> None:
        """When npm returns empty string, no observation is created."""
        with patch.object(
            CCVersionCollector, "_get_registry_version",
            new_callable=AsyncMock, return_value="",
        ):
            await collector._check_registry_version("1.0.0")

        cursor = await db.execute(
            "SELECT count(*) FROM observations WHERE type = 'cc_version_available'",
        )
        row = await cursor.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_registry_check_deduped_on_repeat_calls(self, collector, db) -> None:
        """Second call with same newer registry version skips npm and creates no duplicate."""
        with patch.object(
            CCVersionCollector, "_get_registry_version",
            new_callable=AsyncMock, return_value="2.0.0",
        ) as mock_npm:
            await collector._check_registry_version("1.0.0")  # Creates observation
            await collector._check_registry_version("1.0.0")  # Should be a no-op

        # npm called exactly once — gated by unresolved observation on second call
        mock_npm.assert_awaited_once()

        cursor = await db.execute(
            "SELECT count(*) FROM observations WHERE type = 'cc_version_available'",
        )
        row = await cursor.fetchone()
        assert row[0] == 1

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
