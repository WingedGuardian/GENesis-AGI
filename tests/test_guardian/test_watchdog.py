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


class TestHostContainsCommit:
    """Direct coverage of the merge-base ancestry helper. The exit-code mapping
    is load-bearing: a naive `returncode == 0` would fold 128 (host ahead) into
    False and re-introduce a false alarm — the exact bug this fix removes."""

    @pytest.mark.asyncio
    async def test_ancestor_returns_true(self):
        from unittest.mock import MagicMock
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   return_value=MagicMock(returncode=0)):
            assert await wd._host_contains_commit("a915a28", "0a07272") is True

    @pytest.mark.asyncio
    async def test_not_ancestor_returns_false(self):
        from unittest.mock import MagicMock
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   return_value=MagicMock(returncode=1)):
            assert await wd._host_contains_commit("a915a28", "0ld0ld0") is False

    @pytest.mark.asyncio
    async def test_unknown_object_returns_none_not_false(self):
        """exit 128 (host ahead — commit not in the container's git) MUST map to
        None (skip), NOT False (which would false-alarm)."""
        from unittest.mock import MagicMock
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   return_value=MagicMock(returncode=128)):
            assert await wd._host_contains_commit("a915a28", "ffffff0") is None

    @pytest.mark.asyncio
    async def test_subprocess_exception_returns_none(self):
        """A slow/raising git (e.g. TimeoutExpired) skips, never false-alarms."""
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=TimeoutError("slow git")):
            assert await wd._host_contains_commit("a915a28", "0a07272") is None


def _drift_watchdog(deployed_commit: str):
    """Watchdog wired for code-drift tests: mock remote.version() → deployed_commit,
    mock event_bus/outreach. `_host_contains_commit` is stubbed per-test."""
    r = AsyncMock()
    r.version = AsyncMock(return_value={"deployed_commit": deployed_commit})
    event_bus = AsyncMock()
    outreach = AsyncMock()
    wd = GuardianWatchdog(r, event_bus=event_bus, outreach_queue=outreach)
    return wd, r, event_bus, outreach


def _git_side_effect(container_hash: str):
    """subprocess.run side_effect for the two git calls _check_code_drift_inner
    makes before the (stubbed) ancestry check: git log → container_hash, and
    symbolic-ref → 'main'."""
    from unittest.mock import MagicMock

    def _run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return MagicMock(returncode=0, stdout="main\n")
        if "log" in cmd:
            return MagicMock(returncode=0, stdout=container_hash + "\n")
        return MagicMock(returncode=0, stdout="")

    return _run


class TestCodeDrift:
    """Drift = host's deployed HEAD does NOT CONTAIN the container's latest
    Guardian-path commit. Equality was wrong: a deploy batch with non-Guardian
    commits landing after the last Guardian-touching one leaves HEAD != that
    commit even though HEAD contains it (the false positive this fix removes)."""

    @pytest.mark.asyncio
    async def test_host_contains_commit_no_drift(self):
        """Regression: container's Guardian commit IS an ancestor of host HEAD
        (later non-Guardian commits advanced HEAD) → host current → NO drift,
        NO false Telegram alarm — even after many ticks."""
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="0a07272")
        wd._host_contains_commit = AsyncMock(return_value=True)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_git_side_effect("a915a28")):
            for _ in range(_THRESHOLD + 1):
                await wd._check_code_drift_inner()
        assert wd._drift_count == 0
        outreach.enqueue.assert_not_called()
        assert not any("code_drift" in e for e in _emitted_events(eb))
        wd._host_contains_commit.assert_awaited()  # the ancestry path ran

    @pytest.mark.asyncio
    async def test_genuine_drift_alerts_at_threshold(self):
        """Host genuinely missing the Guardian commit → quiet below threshold,
        alerts exactly at the threshold tick (event bus + user-facing outreach)."""
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="0ld0ld0")
        wd._host_contains_commit = AsyncMock(return_value=False)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_git_side_effect("a915a28")):
            for _ in range(_THRESHOLD - 1):
                await wd._check_code_drift_inner()
            outreach.enqueue.assert_not_called()   # below threshold: silent
            await wd._check_code_drift_inner()       # tick == THRESHOLD
        assert wd._drift_count == _THRESHOLD
        assert any("code_drift" in e for e in _emitted_events(eb))
        outreach.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_unresolvable_skips_no_alarm(self):
        """Ancestry unresolvable (host ahead / git error) → skip every tick,
        never alarm, drift_count untouched."""
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="ffffff0")
        wd._host_contains_commit = AsyncMock(return_value=None)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_git_side_effect("a915a28")):
            for _ in range(_THRESHOLD + 1):
                await wd._check_code_drift_inner()
        assert wd._drift_count == 0
        outreach.enqueue.assert_not_called()
        eb.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_drift_then_resolved_resets(self):
        """A genuine drift episode that later resolves (host catches up) resets
        the counter — no 'stuck drifting' after recovery."""
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="0ld0ld0")
        wd._host_contains_commit = AsyncMock(return_value=False)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_git_side_effect("a915a28")):
            for _ in range(_THRESHOLD):
                await wd._check_code_drift_inner()
            assert wd._drift_count == _THRESHOLD
            wd._host_contains_commit = AsyncMock(return_value=True)  # host caught up
            await wd._check_code_drift_inner()
        assert wd._drift_count == 0

    @pytest.mark.asyncio
    async def test_exit_128_full_chain_skips_no_alarm(self):
        """End-to-end on the most dangerous path: the REAL _host_contains_commit
        (not stubbed) with merge-base exit 128 (host ahead) must map through to a
        skip, never a false alarm. Guards the 128->False misimplementation across
        the whole chain (git 128 -> helper None -> drift check skips)."""
        from unittest.mock import MagicMock
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="ffffff0")

        def _side_effect(cmd, **kw):
            if "symbolic-ref" in cmd:
                return MagicMock(returncode=0, stdout="main\n")
            if "log" in cmd:
                return MagicMock(returncode=0, stdout="a915a28\n")
            if "merge-base" in cmd:
                return MagicMock(returncode=128)  # unknown object — host ahead
            return MagicMock(returncode=0, stdout="")

        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_side_effect):
            for _ in range(_THRESHOLD + 1):
                await wd._check_code_drift_inner()
        assert wd._drift_count == 0
        outreach.enqueue.assert_not_called()
        assert not any("code_drift" in e for e in _emitted_events(eb))
