"""Tests for GuardianWatchdog — bidirectional monitoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.watchdog import GuardianWatchdog
from genesis.observability.health import ProbeResult, ProbeStatus


def _make_probe_result(status: ProbeStatus, staleness_s: float = 0) -> ProbeResult:
    return ProbeResult(
        name="guardian",
        status=status,
        latency_ms=1.0,
        checked_at=datetime.now(UTC).isoformat(),
        details={"staleness_s": staleness_s},
    )


@pytest.fixture
def remote():
    r = AsyncMock()
    r.restart = AsyncMock(return_value=True)
    r.status = AsyncMock(return_value={"current_state": "healthy"})
    r.reset_state = AsyncMock(return_value={"ok": True, "previous_state": "confirmed_dead"})
    return r


@pytest.fixture
def watchdog(remote):
    return GuardianWatchdog(remote, event_bus=None)


class TestCheckAndRecover:
    @pytest.mark.asyncio
    async def test_healthy_no_action(self, watchdog, remote):
        with patch(
            "genesis.observability.health.probe_guardian",
            return_value=_make_probe_result(ProbeStatus.HEALTHY),
        ):
            await watchdog.check_and_recover()
        remote.restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_degraded_no_action(self, watchdog, remote):
        with patch(
            "genesis.observability.health.probe_guardian",
            return_value=_make_probe_result(ProbeStatus.DEGRADED, staleness_s=200),
        ):
            await watchdog.check_and_recover()
        remote.restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_down_triggers_restart(self, watchdog, remote):
        with patch(
            "genesis.observability.health.probe_guardian",
            return_value=_make_probe_result(ProbeStatus.DOWN, staleness_s=600),
        ):
            await watchdog.check_and_recover()
        remote.restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_second_restart(self, watchdog, remote):
        probe_down = _make_probe_result(ProbeStatus.DOWN, staleness_s=600)
        with patch("genesis.observability.health.probe_guardian", return_value=probe_down):
            await watchdog.check_and_recover()  # First — triggers restart
            await watchdog.check_and_recover()  # Second — should be in cooldown
        assert remote.restart.call_count == 1

    @pytest.mark.asyncio
    async def test_cooldown_expires(self, watchdog, remote):
        probe_down = _make_probe_result(ProbeStatus.DOWN, staleness_s=600)
        with patch("genesis.observability.health.probe_guardian", return_value=probe_down):
            await watchdog.check_and_recover()
            # Simulate cooldown expiry
            watchdog._last_recovery_at = datetime.now(UTC) - timedelta(
                seconds=GuardianWatchdog.RECOVERY_COOLDOWN_S + 1,
            )
            await watchdog.check_and_recover()
        assert remote.restart.call_count == 2

    @pytest.mark.asyncio
    async def test_restart_failure_still_sets_cooldown(self, watchdog, remote):
        remote.restart.return_value = False
        with patch(
            "genesis.observability.health.probe_guardian",
            return_value=_make_probe_result(ProbeStatus.DOWN, staleness_s=600),
        ):
            await watchdog.check_and_recover()
        assert watchdog._last_recovery_at is not None


class TestEventEmission:
    @pytest.mark.asyncio
    async def test_emits_event_on_success(self):
        event_bus = AsyncMock()
        r = AsyncMock()
        r.restart.return_value = True
        r.status.return_value = {"current_state": "healthy"}
        wd = GuardianWatchdog(r, event_bus=event_bus)
        with patch(
            "genesis.observability.health.probe_guardian",
            return_value=_make_probe_result(ProbeStatus.DOWN, staleness_s=600),
        ):
            await wd.check_and_recover()
        event_bus.emit.assert_called_once()
        args = event_bus.emit.call_args
        assert "recovery.attempted" in args[0][2]

    @pytest.mark.asyncio
    async def test_emits_error_event_on_failure(self):
        event_bus = AsyncMock()
        r = AsyncMock()
        r.restart.return_value = False
        r.status.return_value = {"current_state": "healthy"}
        wd = GuardianWatchdog(r, event_bus=event_bus)
        with patch(
            "genesis.observability.health.probe_guardian",
            return_value=_make_probe_result(ProbeStatus.DOWN, staleness_s=600),
        ):
            await wd.check_and_recover()
        event_bus.emit.assert_called_once()
        args = event_bus.emit.call_args
        assert "recovery.failed" in args[0][2]


class TestStuckDetection:
    """Test container-side detection of Guardian stuck in confirmed_dead."""

    @pytest.mark.asyncio
    async def test_single_tick_no_reset(self, watchdog, remote):
        """One tick seeing confirmed_dead should NOT trigger reset."""
        remote.status.return_value = {"current_state": "confirmed_dead"}
        probe_down = _make_probe_result(ProbeStatus.DOWN, staleness_s=600)
        with patch("genesis.observability.health.probe_guardian", return_value=probe_down):
            await watchdog.check_and_recover()
        remote.reset_state.assert_not_called()
        assert watchdog._consecutive_stuck == 1

    @pytest.mark.asyncio
    async def test_two_ticks_triggers_reset(self, watchdog, remote):
        """Two consecutive ticks seeing confirmed_dead should trigger reset."""
        remote.status.return_value = {"current_state": "confirmed_dead"}
        probe_down = _make_probe_result(ProbeStatus.DOWN, staleness_s=600)
        with patch("genesis.observability.health.probe_guardian", return_value=probe_down):
            await watchdog.check_and_recover()
            # Expire restart cooldown so second tick can attempt restart
            watchdog._last_recovery_at = datetime.now(UTC) - timedelta(
                seconds=GuardianWatchdog.RECOVERY_COOLDOWN_S + 1,
            )
            await watchdog.check_and_recover()
        remote.reset_state.assert_called_once()
        # Tick 1: restart, Tick 2: restart + post-reset restart = 3 total
        assert remote.restart.call_count == 3

    @pytest.mark.asyncio
    async def test_healthy_clears_stuck_counter(self, watchdog, remote):
        """Healthy probe should clear the consecutive stuck counter."""
        remote.status.return_value = {"current_state": "confirmed_dead"}
        probe_down = _make_probe_result(ProbeStatus.DOWN, staleness_s=600)
        probe_healthy = _make_probe_result(ProbeStatus.HEALTHY)

        with patch("genesis.observability.health.probe_guardian", return_value=probe_down):
            await watchdog.check_and_recover()
        assert watchdog._consecutive_stuck == 1

        with patch("genesis.observability.health.probe_guardian", return_value=probe_healthy):
            await watchdog.check_and_recover()
        assert watchdog._consecutive_stuck == 0

    @pytest.mark.asyncio
    async def test_reset_cooldown_respected(self, watchdog, remote):
        """Reset should not fire again within RESET_COOLDOWN_S."""
        remote.status.return_value = {"current_state": "confirmed_dead"}
        probe_down = _make_probe_result(ProbeStatus.DOWN, staleness_s=600)

        with patch("genesis.observability.health.probe_guardian", return_value=probe_down):
            # Two ticks to trigger first reset
            await watchdog.check_and_recover()
            watchdog._last_recovery_at = datetime.now(UTC) - timedelta(
                seconds=GuardianWatchdog.RECOVERY_COOLDOWN_S + 1,
            )
            await watchdog.check_and_recover()
            assert remote.reset_state.call_count == 1

            # Third tick — should be in reset cooldown
            watchdog._last_recovery_at = datetime.now(UTC) - timedelta(
                seconds=GuardianWatchdog.RECOVERY_COOLDOWN_S + 1,
            )
            await watchdog.check_and_recover()
            # Still 1 — reset cooldown blocks second reset
            assert remote.reset_state.call_count == 1

    @pytest.mark.asyncio
    async def test_non_stuck_state_no_reset(self, watchdog, remote):
        """States other than confirmed_dead/recovering/recovered should not increment."""
        remote.status.return_value = {"current_state": "confirming"}
        probe_down = _make_probe_result(ProbeStatus.DOWN, staleness_s=600)
        with patch("genesis.observability.health.probe_guardian", return_value=probe_down):
            await watchdog.check_and_recover()
            watchdog._last_recovery_at = datetime.now(UTC) - timedelta(
                seconds=GuardianWatchdog.RECOVERY_COOLDOWN_S + 1,
            )
            await watchdog.check_and_recover()
        remote.reset_state.assert_not_called()
        assert watchdog._consecutive_stuck == 0
