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


# sha256 hexdigests are 64 chars; values are arbitrary for the staleness logic.
_EXPECTED_GW_SHA = "a" * 64
_STALE_GW_SHA = "b" * 64
_THRESHOLD = GuardianWatchdog.DRIFT_ALERT_THRESHOLD


def _gw_watchdog(sync_ok: bool = True):
    """Watchdog wired with mock remote/event_bus/outreach and a stubbed
    expected-gateway-sha (so tests don't touch the real container git)."""
    r = AsyncMock()
    r.sync_gateway = AsyncMock(return_value={"ok": sync_ok})
    event_bus = AsyncMock()
    outreach = AsyncMock()
    wd = GuardianWatchdog(r, event_bus=event_bus, outreach_queue=outreach)
    wd._expected_gateway_sha = AsyncMock(return_value=_EXPECTED_GW_SHA)
    return wd, r, event_bus, outreach


def _emitted_events(event_bus) -> list[str]:
    return [c.args[2] for c in event_bus.emit.call_args_list]


class TestGatewayStaleness:
    """Deployed-gateway staleness detection + guarded sync-gateway self-heal."""

    @pytest.mark.asyncio
    async def test_match_no_action(self):
        wd, r, eb, outreach = _gw_watchdog()
        await wd._check_gateway_staleness(
            {"gateway_sha": _EXPECTED_GW_SHA, "code_version": "abc1234"})
        r.sync_gateway.assert_not_called()
        eb.emit.assert_not_called()
        assert wd._gateway_drift_count == 0

    @pytest.mark.asyncio
    async def test_unknown_gateway_sha_skips(self):
        """Host gateway too old to report gateway_sha → never alarm."""
        wd, r, _, _ = _gw_watchdog()
        await wd._check_gateway_staleness(
            {"gateway_sha": "unknown", "code_version": "abc1234"})
        r.sync_gateway.assert_not_called()
        assert wd._gateway_drift_count == 0

    @pytest.mark.asyncio
    async def test_unresolvable_expected_skips(self):
        """Container can't resolve the host's commit (host ahead) → no false alarm."""
        wd, r, _, _ = _gw_watchdog()
        wd._expected_gateway_sha = AsyncMock(return_value=None)
        await wd._check_gateway_staleness(
            {"gateway_sha": _STALE_GW_SHA, "code_version": "abc1234"})
        r.sync_gateway.assert_not_called()
        assert wd._gateway_drift_count == 0

    @pytest.mark.asyncio
    async def test_stale_below_threshold_no_resync(self):
        wd, r, _, _ = _gw_watchdog()
        vi = {"gateway_sha": _STALE_GW_SHA, "code_version": "abc1234"}
        for _ in range(_THRESHOLD - 1):
            await wd._check_gateway_staleness(vi)
        r.sync_gateway.assert_not_called()
        assert wd._gateway_drift_count == _THRESHOLD - 1

    @pytest.mark.asyncio
    async def test_threshold_triggers_single_resync(self):
        wd, r, eb, outreach = _gw_watchdog()
        vi = {"gateway_sha": _STALE_GW_SHA, "code_version": "abc1234"}
        for _ in range(_THRESHOLD):
            await wd._check_gateway_staleness(vi)
        r.sync_gateway.assert_called_once()
        assert any("gateway_resync" in e for e in _emitted_events(eb))
        outreach.enqueue.assert_not_called()  # not escalated yet

    @pytest.mark.asyncio
    async def test_still_stale_after_resync_escalates_once(self):
        wd, r, eb, _ = _gw_watchdog()
        vi = {"gateway_sha": _STALE_GW_SHA, "code_version": "abc1234"}
        for _ in range(_THRESHOLD):
            await wd._check_gateway_staleness(vi)   # → one resync
        await wd._check_gateway_staleness(vi)        # still stale → escalate (event bus)
        await wd._check_gateway_staleness(vi)        # quiet — no second escalation
        stale_events = [e for e in _emitted_events(eb) if "gateway_stale" in e]
        assert len(stale_events) == 1               # escalated exactly once
        r.sync_gateway.assert_called_once()          # resync attempted exactly once

    @pytest.mark.asyncio
    async def test_resolved_after_resync_resets_state(self):
        wd, r, _, _ = _gw_watchdog()
        vi_stale = {"gateway_sha": _STALE_GW_SHA, "code_version": "abc1234"}
        for _ in range(_THRESHOLD):
            await wd._check_gateway_staleness(vi_stale)
        r.sync_gateway.assert_called_once()
        # Next tick: sync worked → deployed sha now matches expected.
        await wd._check_gateway_staleness(
            {"gateway_sha": _EXPECTED_GW_SHA, "code_version": "abc1234"})
        assert wd._gateway_drift_count == 0
        assert wd._gateway_resync_attempted is False
        assert wd._gateway_escalated is False

    @pytest.mark.asyncio
    async def test_rearms_after_resolution(self):
        """After a resolved episode, a fresh staleness episode must self-heal
        again (the most safety-critical transition — no 'quiet forever')."""
        wd, r, _, _ = _gw_watchdog()
        vi_stale = {"gateway_sha": _STALE_GW_SHA, "code_version": "abc1234"}
        vi_ok = {"gateway_sha": _EXPECTED_GW_SHA, "code_version": "abc1234"}
        for _ in range(_THRESHOLD):
            await wd._check_gateway_staleness(vi_stale)   # episode 1 → resync
        await wd._check_gateway_staleness(vi_ok)           # resolved → reset
        assert r.sync_gateway.call_count == 1
        for _ in range(_THRESHOLD):
            await wd._check_gateway_staleness(vi_stale)   # episode 2 → re-armed
        assert r.sync_gateway.call_count == 2


class TestExpectedGatewaySha:
    """Direct coverage of the git-show + sha256 helper (stubbed in the staleness
    tests). A wrong repo path / git failure silently disables detection, so the
    skip paths matter."""

    @pytest.mark.asyncio
    async def test_success_returns_sha(self):
        import hashlib
        from unittest.mock import MagicMock
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        content = b"#!/usr/bin/env bash\n# gateway\n"
        fake = MagicMock(returncode=0, stdout=content)
        with patch("genesis.guardian.watchdog.subprocess.run", return_value=fake):
            sha = await wd._expected_gateway_sha("abc1234")
        assert sha == hashlib.sha256(content).hexdigest()

    @pytest.mark.asyncio
    async def test_nonzero_returncode_returns_none(self):
        from unittest.mock import MagicMock
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        fake = MagicMock(returncode=128, stdout=b"")
        with patch("genesis.guardian.watchdog.subprocess.run", return_value=fake):
            assert await wd._expected_gateway_sha("deadbeef") is None

    @pytest.mark.asyncio
    async def test_empty_stdout_returns_none(self):
        from unittest.mock import MagicMock
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        fake = MagicMock(returncode=0, stdout=b"")
        with patch("genesis.guardian.watchdog.subprocess.run", return_value=fake):
            assert await wd._expected_gateway_sha("abc1234") is None
