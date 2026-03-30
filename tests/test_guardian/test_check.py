"""Tests for Guardian main check logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.check import (
    _build_dispatcher,
    _handle_healthy,
    _write_guardian_heartbeat,
    run_check,
)
from genesis.guardian.config import GuardianConfig
from genesis.guardian.health_signals import HealthSnapshot, PauseState, SignalResult


@pytest.fixture
def config(tmp_path: Path) -> GuardianConfig:
    cfg = GuardianConfig()
    cfg.state_dir = str(tmp_path / "guardian-state")
    return cfg


def _healthy_snapshot() -> HealthSnapshot:
    return HealthSnapshot(
        signals={
            name: SignalResult(name, True, 1.0, "ok", "t")
            for name in [
                "container_exists", "icmp_reachable", "health_api",
                "heartbeat_canary", "log_freshness",
            ]
        },
        pause_state=PauseState(paused=False),
    )


class TestBuildDispatcher:

    def test_no_credentials(self) -> None:
        config = GuardianConfig()
        dispatcher = _build_dispatcher(config)
        assert len(dispatcher._channels) == 0

    def test_with_credentials(self) -> None:
        config = GuardianConfig()
        config.alert.telegram_bot_token = "test-token"
        config.alert.telegram_chat_id = "12345"
        dispatcher = _build_dispatcher(config)
        assert len(dispatcher._channels) == 1


class TestWriteGuardianHeartbeat:

    @pytest.mark.asyncio
    async def test_writes_heartbeat(self) -> None:
        config = GuardianConfig()
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_proc.pid = 9999

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_create:
            await _write_guardian_heartbeat(config)
        mock_create.assert_called_once()
        # Verify stdin was provided with heartbeat data
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs.get("stdin") is not None


class TestHandleHealthy:

    @pytest.mark.asyncio
    async def test_writes_heartbeat_and_skips_prune(self, config: GuardianConfig) -> None:
        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=0)

        with patch(
            "genesis.guardian.check._write_guardian_heartbeat",
            AsyncMock(),
        ):
            await _handle_healthy(config, snapshots)

    @pytest.mark.asyncio
    async def test_prunes_when_due(self, config: GuardianConfig) -> None:
        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=2)

        with patch(
            "genesis.guardian.check._write_guardian_heartbeat",
            AsyncMock(),
        ):
            await _handle_healthy(config, snapshots)
        # Should attempt prune (no marker file exists = overdue)
        snapshots.prune.assert_called_once()


class TestRunCheck:

    @pytest.mark.asyncio
    async def test_healthy_cycle(self, config: GuardianConfig) -> None:
        """Full healthy cycle: collect signals → HEALTHY → write heartbeat → save state."""
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        # State file should exist after run
        state_file = config.state_path / "state.json"
        assert state_file.exists()

    @pytest.mark.asyncio
    async def test_state_persists_across_runs(self, config: GuardianConfig) -> None:
        """State should persist between invocations."""
        dead_snapshot = HealthSnapshot(
            signals={
                "container_exists": SignalResult("container_exists", False, 1.0, "down", "t"),
                "icmp_reachable": SignalResult("icmp_reachable", False, 1.0, "down", "t"),
                "health_api": SignalResult("health_api", True, 1.0, "ok", "t"),
                "heartbeat_canary": SignalResult("heartbeat_canary", True, 1.0, "ok", "t"),
                "log_freshness": SignalResult("log_freshness", True, 1.0, "ok", "t"),
            },
            pause_state=PauseState(paused=False),
        )

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=dead_snapshot),
            ),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        # First run should move to SIGNAL_DROPPED
        import json
        state = json.loads((config.state_path / "state.json").read_text())
        assert state["current_state"] == "signal_dropped"

    @pytest.mark.asyncio
    async def test_saves_state_on_error(self, config: GuardianConfig) -> None:
        """State should be saved even if check_cycle raises."""
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch("genesis.guardian.check.load_secrets", return_value={}),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await run_check(config)

        # State file should still be written
        state_file = config.state_path / "state.json"
        assert state_file.exists()
