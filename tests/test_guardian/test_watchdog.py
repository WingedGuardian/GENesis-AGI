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
        remote = AsyncMock()
        remote.restart.return_value = True
        wd = GuardianWatchdog(remote, event_bus=event_bus)
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
        remote = AsyncMock()
        remote.restart.return_value = False
        wd = GuardianWatchdog(remote, event_bus=event_bus)
        with patch(
            "genesis.observability.health.probe_guardian",
            return_value=_make_probe_result(ProbeStatus.DOWN, staleness_s=600),
        ):
            await wd.check_and_recover()
        event_bus.emit.assert_called_once()
        args = event_bus.emit.call_args
        assert "recovery.failed" in args[0][2]
