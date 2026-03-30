"""Tests for StandaloneHealthDataService — health MCP in standalone mode.

Used when health MCP runs as a stdio subprocess (via .mcp.json) rather than
in-process within GenesisRuntime. Reads status.json instead of live probing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def status_json(tmp_path: Path) -> Path:
    """Create a realistic status.json file."""
    data = {
        "timestamp": "2026-03-17T05:15:41.826368+00:00",
        "resilience_state": {
            "cloud": "NORMAL",
            "memory": "NORMAL",
            "embedding": "NORMAL",
            "cc": "NORMAL",
        },
        "queue_depths": {
            "deferred_work": 0,
            "dead_letter": 2,
            "pending_embeddings": 0,
        },
        "last_recovery": None,
        "human_summary": "All systems normal. 2 items queued.",
    }
    path = tmp_path / "status.json"
    path.write_text(json.dumps(data, indent=2))
    return path


@pytest.fixture
def degraded_status_json(tmp_path: Path) -> Path:
    """Status file showing degraded state."""
    data = {
        "timestamp": "2026-03-17T06:00:00+00:00",
        "resilience_state": {
            "cloud": "DEGRADED",
            "memory": "NORMAL",
            "embedding": "FALLBACK",
            "cc": "THROTTLED",
        },
        "queue_depths": {
            "deferred_work": 5,
            "dead_letter": 12,
            "pending_embeddings": 3,
        },
        "last_recovery": "2026-03-17T05:55:00+00:00",
        "human_summary": "Cloud degraded, Embedding fallback. 20 items queued.",
    }
    path = tmp_path / "status.json"
    path.write_text(json.dumps(data, indent=2))
    return path


class TestStandaloneHealthDataService:
    """Test the standalone health data service that reads status.json."""

    async def test_snapshot_reads_status_file(self, status_json: Path) -> None:
        from genesis.mcp.standalone_health import StandaloneHealthDataService

        svc = StandaloneHealthDataService(status_path=status_json, db=None)
        snap = await svc.snapshot()

        assert snap["resilience_state"]["cloud"] == "NORMAL"
        assert snap["resilience_state"]["cc"] == "NORMAL"
        assert snap["queue_depths"]["dead_letter"] == 2
        assert snap["human_summary"] == "All systems normal. 2 items queued."

    async def test_snapshot_degraded_state(self, degraded_status_json: Path) -> None:
        from genesis.mcp.standalone_health import StandaloneHealthDataService

        svc = StandaloneHealthDataService(status_path=degraded_status_json, db=None)
        snap = await svc.snapshot()

        assert snap["resilience_state"]["cloud"] == "DEGRADED"
        assert snap["resilience_state"]["embedding"] == "FALLBACK"
        assert snap["queue_depths"]["deferred_work"] == 5

    async def test_snapshot_missing_file(self, tmp_path: Path) -> None:
        from genesis.mcp.standalone_health import StandaloneHealthDataService

        svc = StandaloneHealthDataService(
            status_path=tmp_path / "nonexistent.json", db=None,
        )
        snap = await svc.snapshot()

        assert snap["status"] == "unavailable"
        assert "not found" in snap["message"].lower()

    async def test_snapshot_corrupt_json(self, tmp_path: Path) -> None:
        from genesis.mcp.standalone_health import StandaloneHealthDataService

        bad_path = tmp_path / "status.json"
        bad_path.write_text("{invalid json")

        svc = StandaloneHealthDataService(status_path=bad_path, db=None)
        snap = await svc.snapshot()

        assert snap["status"] == "unavailable"
        assert "parse" in snap["message"].lower() or "json" in snap["message"].lower()

    async def test_health_status_uses_standalone_snapshot(
        self, status_json: Path,
    ) -> None:
        """When wired with StandaloneHealthDataService, health_status returns file data."""
        import genesis.mcp.health_mcp as health_mcp_mod
        from genesis.mcp.standalone_health import StandaloneHealthDataService

        svc = StandaloneHealthDataService(status_path=status_json, db=None)
        old = health_mcp_mod._service
        try:
            health_mcp_mod._service = svc
            result = await health_mcp_mod._impl_health_status()
            # Should NOT be "unavailable" — it should have real data
            assert result.get("status") != "unavailable"
            assert "infrastructure" in result or "resilience_state" in result
        finally:
            health_mcp_mod._service = old


class TestStandaloneBootstrapAndJobHealth:
    """Test that runtime-dependent tools degrade gracefully in standalone mode."""

    async def test_bootstrap_manifest_does_not_crash(self) -> None:
        """bootstrap_manifest should never crash, even without a running runtime."""
        from genesis.mcp.health_mcp import _impl_bootstrap_manifest

        result = await _impl_bootstrap_manifest()
        # Should return a dict (either real data or graceful unavailable)
        assert isinstance(result, dict)

    async def test_job_health_does_not_crash(self) -> None:
        """job_health should never crash, even without a running runtime."""
        from genesis.mcp.health_mcp import _impl_job_health

        result = await _impl_job_health()
        assert isinstance(result, dict)


class TestMCPServerWrapper:
    """Test the genesis_mcp_server.py wrapper bootstrap logic."""

    def test_parse_server_arg_health(self) -> None:
        from scripts.genesis_mcp_server import parse_args

        args = parse_args(["--server", "health"])
        assert args.server == "health"

    def test_parse_server_arg_memory(self) -> None:
        from scripts.genesis_mcp_server import parse_args

        args = parse_args(["--server", "memory"])
        assert args.server == "memory"

    def test_parse_server_arg_outreach(self) -> None:
        from scripts.genesis_mcp_server import parse_args

        args = parse_args(["--server", "outreach"])
        assert args.server == "outreach"

    def test_parse_server_arg_recon(self) -> None:
        from scripts.genesis_mcp_server import parse_args

        args = parse_args(["--server", "recon"])
        assert args.server == "recon"

    def test_parse_server_arg_invalid(self) -> None:
        from scripts.genesis_mcp_server import parse_args

        with pytest.raises(SystemExit):
            parse_args(["--server", "invalid"])

    def test_flag_check_disabled(self, tmp_path: Path) -> None:
        from scripts.genesis_mcp_server import is_genesis_enabled

        # Flag file doesn't exist → disabled
        assert is_genesis_enabled(flag_path=tmp_path / "nonexistent") is False

    def test_flag_check_enabled(self, tmp_path: Path) -> None:
        from scripts.genesis_mcp_server import is_genesis_enabled

        flag = tmp_path / "cc_context_enabled"
        flag.touch()
        assert is_genesis_enabled(flag_path=flag) is True


class TestHealthBootstrapLifespan:
    """Test that health bootstrap opens and closes DB via lifespan."""

    async def test_bootstrap_uses_lifespan_when_db_exists(
        self, tmp_path: Path,
    ) -> None:
        """When DB file exists, _bootstrap_health sets mcp._lifespan."""
        from unittest.mock import MagicMock, patch

        # Create a fake DB file so the path check passes
        fake_db = tmp_path / "genesis.db"
        fake_db.touch()

        mock_mcp = MagicMock()
        mock_mcp._lifespan = None

        with (
            patch("scripts.genesis_mcp_server._DEFAULT_DB", fake_db),
            patch("scripts.genesis_mcp_server._DEFAULT_STATUS", tmp_path / "status.json"),
            patch("genesis.mcp.health_mcp.mcp", mock_mcp),
        ):
            from scripts.genesis_mcp_server import _bootstrap_health

            _bootstrap_health()

        # _lifespan was set (not None) and run was called
        assert mock_mcp._lifespan is not None
        mock_mcp.run.assert_called_once_with(transport="stdio")

    async def test_bootstrap_no_lifespan_when_db_missing(
        self, tmp_path: Path,
    ) -> None:
        """When DB file is missing, falls back to db=None (no lifespan)."""
        from unittest.mock import MagicMock, patch

        mock_mcp = MagicMock()

        with (
            patch("scripts.genesis_mcp_server._DEFAULT_DB", tmp_path / "nonexistent.db"),
            patch("scripts.genesis_mcp_server._DEFAULT_STATUS", tmp_path / "status.json"),
            patch("genesis.mcp.health_mcp.mcp", mock_mcp),
        ):
            from scripts.genesis_mcp_server import _bootstrap_health

            _bootstrap_health()

        # Should still call run (db=None fallback)
        mock_mcp.run.assert_called_once_with(transport="stdio")

    async def test_lifespan_opens_and_closes_db(
        self, tmp_path: Path,
    ) -> None:
        """The lifespan context manager opens DB on enter and closes on exit."""
        from unittest.mock import AsyncMock, MagicMock, patch

        fake_db = tmp_path / "genesis.db"
        fake_db.touch()

        mock_mcp = MagicMock()
        mock_mcp._lifespan = None

        mock_conn = AsyncMock()
        mock_conn.row_factory = None
        mock_conn.execute = AsyncMock()

        # Keep the patch active for the entire test — the lifespan closure
        # reads _DEFAULT_DB from the module namespace at execution time.
        with (
            patch("scripts.genesis_mcp_server._DEFAULT_DB", fake_db),
            patch("scripts.genesis_mcp_server._DEFAULT_STATUS", tmp_path / "status.json"),
            patch("genesis.mcp.health_mcp.mcp", mock_mcp),
        ):
            from scripts.genesis_mcp_server import _bootstrap_health

            _bootstrap_health()

            # Extract the lifespan that was set
            lifespan = mock_mcp._lifespan

            # Test the lifespan context manager actually opens/closes
            with patch("aiosqlite.connect", new_callable=AsyncMock) as mock_connect:
                mock_connect.return_value = mock_conn
                async with lifespan(mock_mcp):
                    # DB should be connected and PRAGMAs set
                    mock_connect.assert_awaited_once_with(str(fake_db))
                    mock_conn.execute.assert_any_await("PRAGMA journal_mode=WAL")
                    from genesis.db.connection import BUSY_TIMEOUT_MS

                    mock_conn.execute.assert_any_await(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

                # DB should be closed after exit
                mock_conn.close.assert_awaited_once()


class TestStandaloneServiceDetection:
    """Test that standalone snapshot includes systemd service status."""

    async def test_snapshot_includes_services(self, status_json: Path) -> None:
        """snapshot() should include a services dict from collect_service_status."""
        from unittest.mock import patch

        from genesis.mcp.standalone_health import StandaloneHealthDataService

        mock_services = {
            "bridge": {
                "active_state": "active",
                "sub_state": "running",
                "pid": 12345,
                "pid_alive": True,
            },
            "watchdog_timer": {
                "active_state": "active",
                "sub_state": "waiting",
            },
        }

        with patch(
            "genesis.observability.service_status.collect_service_status",
            return_value=mock_services,
        ):
            svc = StandaloneHealthDataService(status_path=status_json, db=None)
            snap = await svc.snapshot()

        assert "services" in snap
        assert snap["services"]["bridge"]["active_state"] == "active"
        assert snap["services"]["bridge"]["pid"] == 12345
        assert snap["services"]["watchdog_timer"]["active_state"] == "active"

    async def test_snapshot_services_degrades_gracefully(
        self, status_json: Path,
    ) -> None:
        """If collect_service_status raises, services should be empty dict."""
        from unittest.mock import patch

        from genesis.mcp.standalone_health import StandaloneHealthDataService

        with patch(
            "genesis.observability.service_status.collect_service_status",
            side_effect=RuntimeError("systemctl not available"),
        ):
            svc = StandaloneHealthDataService(status_path=status_json, db=None)
            snap = await svc.snapshot()

        assert "services" in snap
        assert snap["services"] == {}


class TestHeartbeatQueriesWithDB:
    """Test that heartbeat queries work when DB is connected."""

    async def test_heartbeats_find_events_in_db(self, db) -> None:
        """With a real DB containing heartbeat events, subsystem_heartbeats
        should return alive status."""
        from datetime import UTC, datetime

        import genesis.mcp.health_mcp as health_mcp_mod
        from genesis.db.crud import events as events_crud
        from genesis.mcp.health_mcp import _impl_subsystem_heartbeats
        from genesis.mcp.standalone_health import StandaloneHealthDataService

        # Insert a recent heartbeat event for the awareness subsystem
        ts = datetime.now(UTC).isoformat()
        await events_crud.insert(
            db,
            subsystem="awareness",
            severity="debug",
            event_type="heartbeat",
            message="awareness tick complete",
            timestamp=ts,
        )

        svc = StandaloneHealthDataService(
            status_path="/tmp/nonexistent.json", db=db,
        )

        old_service = health_mcp_mod._service
        try:
            health_mcp_mod._service = svc
            result = await _impl_subsystem_heartbeats()

            # awareness should show alive (just inserted)
            assert result["awareness"]["status"] == "alive"
            assert result["awareness"]["last_seen"] == ts

            # surplus should show no_heartbeat (no event inserted)
            assert result["surplus"]["status"] == "no_heartbeat"
        finally:
            health_mcp_mod._service = old_service

    async def test_heartbeats_no_db_returns_no_heartbeat(self) -> None:
        """Without DB, all subsystems should report no_heartbeat."""
        import genesis.mcp.health_mcp as health_mcp_mod
        from genesis.mcp.health_mcp import _impl_subsystem_heartbeats
        from genesis.mcp.standalone_health import StandaloneHealthDataService

        svc = StandaloneHealthDataService(
            status_path="/tmp/nonexistent.json", db=None,
        )

        old_service = health_mcp_mod._service
        old_event_bus = health_mcp_mod._event_bus
        try:
            health_mcp_mod._service = svc
            health_mcp_mod._event_bus = None
            result = await _impl_subsystem_heartbeats()

            for name in ("awareness", "surplus", "inbox", "reflection", "outreach", "dashboard"):
                assert result[name]["status"] == "no_heartbeat"
                assert result[name]["last_seen"] is None
        finally:
            health_mcp_mod._service = old_service
            health_mcp_mod._event_bus = old_event_bus

    async def test_heartbeat_overdue_detection(self, db) -> None:
        """Old heartbeat event should show overdue status."""
        from datetime import UTC, datetime, timedelta

        import genesis.mcp.health_mcp as health_mcp_mod
        from genesis.db.crud import events as events_crud
        from genesis.mcp.health_mcp import _impl_subsystem_heartbeats
        from genesis.mcp.standalone_health import StandaloneHealthDataService

        # Insert an OLD heartbeat (2 hours ago — well past the 360s overdue threshold)
        old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await events_crud.insert(
            db,
            subsystem="awareness",
            severity="debug",
            event_type="heartbeat",
            message="awareness tick complete",
            timestamp=old_ts,
        )

        svc = StandaloneHealthDataService(
            status_path="/tmp/nonexistent.json", db=db,
        )

        old_service = health_mcp_mod._service
        try:
            health_mcp_mod._service = svc
            result = await _impl_subsystem_heartbeats()

            assert result["awareness"]["status"] == "overdue"
            assert result["awareness"]["last_seen"] == old_ts
            assert result["awareness"]["age_seconds"] > 600
        finally:
            health_mcp_mod._service = old_service


class TestStatusWriterAtomicWrite:
    """Test that status.json is written atomically (temp + rename)."""

    async def test_atomic_write(self, tmp_path: Path) -> None:
        """StatusFileWriter should use atomic write (temp file + os.replace)."""
        from genesis.resilience.state import (
            CCStatus,
            CloudStatus,
            EmbeddingStatus,
            MemoryStatus,
            ResilienceState,
        )
        from genesis.resilience.status_writer import StatusFileWriter

        state = ResilienceState(
            cloud=CloudStatus.NORMAL,
            memory=MemoryStatus.NORMAL,
            embedding=EmbeddingStatus.NORMAL,
            cc=CCStatus.NORMAL,
        )

        class FakeStateMachine:
            current = state

        status_path = tmp_path / "status.json"
        writer = StatusFileWriter(
            state_machine=FakeStateMachine(),
            path=str(status_path),
        )

        await writer.write()

        # File should exist and be valid JSON
        assert status_path.exists()
        data = json.loads(status_path.read_text())
        assert "timestamp" in data
        assert "resilience_state" in data
        assert data["resilience_state"]["cloud"] == "NORMAL"

    async def test_atomic_write_no_partial_reads(self, tmp_path: Path) -> None:
        """No .tmp files should remain after successful write."""
        import os

        from genesis.resilience.state import (
            CCStatus,
            CloudStatus,
            EmbeddingStatus,
            MemoryStatus,
            ResilienceState,
        )
        from genesis.resilience.status_writer import StatusFileWriter

        state = ResilienceState(
            cloud=CloudStatus.NORMAL,
            memory=MemoryStatus.NORMAL,
            embedding=EmbeddingStatus.NORMAL,
            cc=CCStatus.NORMAL,
        )

        class FakeStateMachine:
            current = state

        status_path = tmp_path / "status.json"
        writer = StatusFileWriter(
            state_machine=FakeStateMachine(),
            path=str(status_path),
        )

        await writer.write()

        # No temp files should remain
        tmp_files = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
        assert tmp_files == [], f"Temp files not cleaned up: {tmp_files}"


class TestEnrichFromDB:
    """Test that _enrich_from_db populates real data from the shared DB."""

    async def test_enrich_populates_cc_sessions(self, db, status_json: Path) -> None:
        """cc_sessions should return structured data (not empty) with a real DB."""
        from unittest.mock import patch

        from genesis.mcp.standalone_health import StandaloneHealthDataService

        with patch(
            "genesis.observability.service_status.collect_service_status",
            return_value={},
        ):
            svc = StandaloneHealthDataService(status_path=status_json, db=db)
            snap = await svc.snapshot()

        # cc_sessions should be a dict with structured data, not a placeholder
        assert isinstance(snap["cc_sessions"], dict)
        assert snap["cc_sessions"] != {"note": "standalone mode — no live session data"}

    async def test_enrich_populates_call_sites(self, db, status_json: Path) -> None:
        """call_sites should reflect data from call_site_last_run table."""
        from unittest.mock import patch

        from genesis.mcp.standalone_health import StandaloneHealthDataService

        # Insert a call site record
        await db.execute(
            "INSERT OR REPLACE INTO call_site_last_run "
            "(call_site_id, last_run_at, provider_used, model_id, "
            "input_tokens, output_tokens, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test_site", "2026-03-27T04:00:00+00:00", "openrouter", "gpt-4",
             100, 50, "2026-03-27T04:00:00+00:00"),
        )
        await db.commit()

        with patch(
            "genesis.observability.service_status.collect_service_status",
            return_value={},
        ):
            svc = StandaloneHealthDataService(status_path=status_json, db=db)
            snap = await svc.snapshot()

        assert "test_site" in snap["call_sites"]
        site = snap["call_sites"]["test_site"]
        assert site["last_provider"] == "openrouter"
        assert site["last_model"] == "gpt-4"
        assert site["last_tokens"] == 150

    async def test_enrich_failure_does_not_corrupt_snapshot(
        self, status_json: Path,
    ) -> None:
        """If enrichment fails, the snapshot should still return valid data."""
        from unittest.mock import AsyncMock, patch

        from genesis.mcp.standalone_health import StandaloneHealthDataService

        # Create a mock DB that raises on execute
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=RuntimeError("DB broken"))

        with patch(
            "genesis.observability.service_status.collect_service_status",
            return_value={},
        ):
            svc = StandaloneHealthDataService(status_path=status_json, db=mock_db)
            snap = await svc.snapshot()

        # Snapshot should still be valid with empty defaults
        assert isinstance(snap, dict)
        assert "resilience_state" in snap
        assert isinstance(snap["call_sites"], dict)
        assert isinstance(snap["cost"], dict)

    async def test_enrich_cost_wired_from_cc_sessions(
        self, db, status_json: Path,
    ) -> None:
        """cost field should reflect shadow costs from cc_sessions when available."""
        from unittest.mock import patch

        from genesis.mcp.standalone_health import StandaloneHealthDataService

        with patch(
            "genesis.observability.service_status.collect_service_status",
            return_value={},
        ):
            svc = StandaloneHealthDataService(status_path=status_json, db=db)
            snap = await svc.snapshot()

        # cost should have structured fields, not a placeholder note
        assert isinstance(snap["cost"], dict)
        assert "daily_usd" in snap["cost"]
        assert "budget_status" in snap["cost"]


class TestReflectionHeartbeatEmission:
    """Test that micro-reflection emits a REFLECTION heartbeat."""

    async def test_heartbeat_emitted_on_success(self) -> None:
        """Successful micro-reflection should emit REFLECTION heartbeat."""
        from unittest.mock import AsyncMock, MagicMock

        from genesis.awareness.loop import AwarenessLoop
        from genesis.awareness.types import Depth, TickResult
        from genesis.observability.types import Severity, Subsystem

        mock_engine = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_engine.reflect.return_value = mock_result

        mock_bus = AsyncMock()

        loop = AwarenessLoop.__new__(AwarenessLoop)
        loop._reflection_engine = mock_engine
        loop._event_bus = mock_bus
        loop._cc_reflection_bridge = None
        loop._deferred_queue = None
        loop._db = AsyncMock()

        tick = MagicMock(spec=TickResult)
        tick.classified_depth = Depth.MICRO
        tick.tick_id = "test-tick-123"

        await loop._dispatch_reflection(tick)

        mock_bus.emit.assert_awaited_once_with(
            Subsystem.REFLECTION, Severity.DEBUG,
            "heartbeat", "micro-reflection completed",
        )

    async def test_heartbeat_not_emitted_on_failure(self) -> None:
        """Failed micro-reflection should NOT emit heartbeat."""
        from unittest.mock import AsyncMock, MagicMock

        from genesis.awareness.loop import AwarenessLoop
        from genesis.awareness.types import Depth, TickResult

        mock_engine = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = False
        mock_engine.reflect.return_value = mock_result

        mock_bus = AsyncMock()

        loop = AwarenessLoop.__new__(AwarenessLoop)
        loop._reflection_engine = mock_engine
        loop._event_bus = mock_bus
        loop._cc_reflection_bridge = None
        loop._deferred_queue = None
        loop._db = AsyncMock()

        tick = MagicMock(spec=TickResult)
        tick.classified_depth = Depth.MICRO
        tick.tick_id = "test-tick-456"

        await loop._dispatch_reflection(tick)

        mock_bus.emit.assert_not_awaited()
