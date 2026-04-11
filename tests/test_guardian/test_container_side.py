"""Tests for Guardian container-side integration.

Tests the heartbeat endpoint, guardian-dialogue endpoint,
probe_guardian, AwarenessLoop tick tracking, and infrastructure wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.observability.health import probe_guardian
from genesis.observability.types import ProbeResult, ProbeStatus

# ── probe_guardian ───────────────────────────────────────────────────────


class TestProbeGuardian:

    @pytest.mark.asyncio
    async def test_healthy_heartbeat(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": now.isoformat(),
        }))

        result = await probe_guardian(hb_file, clock=lambda: now)
        assert result.status == ProbeStatus.HEALTHY
        assert result.name == "guardian"

    @pytest.mark.asyncio
    async def test_degraded_heartbeat(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(seconds=150)
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": old.isoformat(),
        }))

        result = await probe_guardian(hb_file, clock=lambda: now)
        assert result.status == ProbeStatus.DEGRADED
        assert "150" in result.message

    @pytest.mark.asyncio
    async def test_down_heartbeat(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(seconds=400)
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": old.isoformat(),
        }))

        result = await probe_guardian(hb_file, clock=lambda: now)
        assert result.status == ProbeStatus.DOWN
        assert "400" in result.message

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path: Path) -> None:
        # Pass guardian_remote=None to skip the SSH fallback that
        # probe_guardian auto-loads from guardian_remote.yaml when the
        # heartbeat file is missing — we want to exercise the local
        # "file missing + no remote" path.
        result = await probe_guardian(
            tmp_path / "nonexistent.json", guardian_remote=None,
        )
        assert result.status == ProbeStatus.DOWN
        assert "not found" in result.message

    @pytest.mark.asyncio
    async def test_corrupt_json(self, tmp_path: Path) -> None:
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text("not json{{{")

        result = await probe_guardian(hb_file)
        assert result.status == ProbeStatus.DEGRADED
        assert "unreadable" in result.message

    @pytest.mark.asyncio
    async def test_missing_timestamp(self, tmp_path: Path) -> None:
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({"guardian_alive": True}))

        result = await probe_guardian(hb_file)
        assert result.status == ProbeStatus.DEGRADED
        assert "timestamp" in result.message

    @pytest.mark.asyncio
    async def test_custom_thresholds(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(seconds=50)
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": old.isoformat(),
        }))

        # With tight thresholds, 50s is degraded
        result = await probe_guardian(
            hb_file,
            degraded_threshold_s=30.0,
            down_threshold_s=60.0,
            clock=lambda: now,
        )
        assert result.status == ProbeStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_paused_returns_degraded(self, tmp_path: Path) -> None:
        """When Genesis is paused, probe returns DEGRADED with 'Guardian paused'."""
        now = datetime.now(UTC)
        old = now - timedelta(seconds=9999)  # Very stale — would be DOWN normally
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": old.isoformat(),
        }))
        # Create paused.json in the same directory
        pause_file = tmp_path / "paused.json"
        pause_file.write_text(json.dumps({"paused": True, "reason": "Dashboard toggle"}))

        result = await probe_guardian(hb_file, clock=lambda: now)
        assert result.status == ProbeStatus.DEGRADED
        assert result.message == "Guardian paused"
        assert result.details.get("paused") is True

    @pytest.mark.asyncio
    async def test_paused_corrupt_json_falls_through(self, tmp_path: Path) -> None:
        """Corrupt paused.json should fall through to normal staleness check."""
        now = datetime.now(UTC)
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": now.isoformat(),
        }))
        pause_file = tmp_path / "paused.json"
        pause_file.write_text("not json{{{")

        result = await probe_guardian(hb_file, clock=lambda: now)
        assert result.status == ProbeStatus.HEALTHY  # Falls through to normal check

    @pytest.mark.asyncio
    async def test_paused_false_still_checks_staleness(self, tmp_path: Path) -> None:
        """paused.json with paused=false should not short-circuit."""
        now = datetime.now(UTC)
        old = now - timedelta(seconds=400)
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": old.isoformat(),
        }))
        pause_file = tmp_path / "paused.json"
        pause_file.write_text(json.dumps({"paused": False}))

        result = await probe_guardian(hb_file, clock=lambda: now)
        assert result.status == ProbeStatus.DOWN  # Normal staleness check

    @pytest.mark.asyncio
    async def test_staleness_in_details(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": now.isoformat(),
        }))

        result = await probe_guardian(hb_file, clock=lambda: now)
        assert result.details is not None
        assert "staleness_s" in result.details


# ── AwarenessLoop tick tracking ──────────────────────────────────────────


class TestAwarenessLoopTickTracking:

    def test_tick_count_starts_at_zero(self) -> None:
        from genesis.awareness.loop import AwarenessLoop

        loop = AwarenessLoop(db=MagicMock(), collectors=[])
        assert loop.tick_count == 0
        assert loop.last_tick_at is None

    @pytest.mark.asyncio
    async def test_tick_increments_counter(self) -> None:
        from genesis.awareness.loop import AwarenessLoop

        loop = AwarenessLoop(db=AsyncMock(), collectors=[])

        # Mock perform_tick to avoid real DB operations
        with patch("genesis.awareness.loop.perform_tick", new_callable=AsyncMock) as mock_tick:
            mock_tick.return_value = MagicMock(
                classified_depth=None, tick_id="test-tick",
            )
            await loop._on_tick()

        assert loop.tick_count == 1
        assert loop.last_tick_at is not None

    @pytest.mark.asyncio
    async def test_multiple_ticks_increment(self) -> None:
        from genesis.awareness.loop import AwarenessLoop

        loop = AwarenessLoop(db=AsyncMock(), collectors=[])

        with patch("genesis.awareness.loop.perform_tick", new_callable=AsyncMock) as mock_tick:
            mock_tick.return_value = MagicMock(
                classified_depth=None, tick_id="test-tick",
            )
            await loop._on_tick()
            first_ts = loop.last_tick_at
            await loop._on_tick()
            await loop._on_tick()

        assert loop.tick_count == 3
        assert loop.last_tick_at is not None
        assert loop.last_tick_at >= first_ts


# ── Infrastructure wiring ────────────────────────────────────────────────


class TestInfrastructureGuardianWiring:

    @pytest.mark.asyncio
    async def test_guardian_appears_in_infrastructure(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        hb_file = tmp_path / "guardian_heartbeat.json"
        hb_file.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": now.isoformat(),
        }))

        healthy_probe = ProbeResult(
            name="guardian",
            status=ProbeStatus.HEALTHY,
            latency_ms=0.1,
            checked_at=now.isoformat(),
        )

        with patch(
            "genesis.observability.snapshots.infrastructure.probe_guardian",
            new_callable=AsyncMock,
            return_value=healthy_probe,
        ):
            from genesis.observability.snapshots.infrastructure import infrastructure

            infra = await infrastructure(
                db=None,
                routing_config=None,
                learning_scheduler=None,
                state_machine=None,
            )

        assert "guardian" in infra
        assert infra["guardian"]["status"] == "healthy"
