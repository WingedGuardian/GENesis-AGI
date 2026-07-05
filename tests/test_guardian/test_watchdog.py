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

    @pytest.mark.asyncio
    async def test_recovery_failed_alerts_once_per_episode_and_rearms(self):
        """Guardian DOWN + SSH restart fails → ONE user alert per DOWN episode
        (even across repeated restart attempts), re-armed when Guardian recovers.
        The most critical escalation: the last line of defense is down."""
        outreach = AsyncMock()
        r = AsyncMock()
        r.restart.return_value = False
        r.status.return_value = {"current_state": "healthy"}
        wd = GuardianWatchdog(r, event_bus=AsyncMock(), outreach_pipeline=outreach)
        wd._check_code_drift = AsyncMock()   # isolate from real git subprocess
        wd._in_cooldown = lambda: False        # allow a restart attempt every tick
        down = _make_probe_result(ProbeStatus.DOWN, staleness_s=600)
        healthy = _make_probe_result(ProbeStatus.HEALTHY)

        with patch("genesis.observability.health.probe_guardian", return_value=down):
            for _ in range(3):                 # 3 down+fail ticks
                await wd.check_and_recover()
        outreach.submit_raw.assert_called_once()   # ONE alert for the episode
        assert outreach.submit_raw.call_args.args[1].source_id == "guardian:recovery_failed"

        with patch("genesis.observability.health.probe_guardian", return_value=healthy):
            await wd.check_and_recover()           # recovers → re-arm
        assert wd._recovery_failed_escalated is False

        with patch("genesis.observability.health.probe_guardian", return_value=down):
            await wd.check_and_recover()           # second episode → second alert
        assert outreach.submit_raw.call_count == 2


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
    wd = GuardianWatchdog(r, event_bus=event_bus, outreach_pipeline=outreach)
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
        outreach.submit_raw.assert_not_called()  # not escalated yet

    @pytest.mark.asyncio
    async def test_still_stale_after_resync_escalates_once(self):
        wd, r, eb, outreach = _gw_watchdog()
        vi = {"gateway_sha": _STALE_GW_SHA, "code_version": "abc1234"}
        for _ in range(_THRESHOLD):
            await wd._check_gateway_staleness(vi)   # → one resync
        await wd._check_gateway_staleness(vi)        # still stale → escalate
        await wd._check_gateway_staleness(vi)        # quiet — no second escalation
        stale_events = [e for e in _emitted_events(eb) if "gateway_stale" in e]
        assert len(stale_events) == 1               # escalated exactly once
        r.sync_gateway.assert_called_once()          # resync attempted exactly once
        outreach.submit_raw.assert_called_once()     # ONE user alert per episode
        req = outreach.submit_raw.call_args.args[1]
        assert req.source_id == "guardian:gateway_stale"

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


def _drift_watchdog(deployed_commit: str, deploy_ref: str | None = "dep10y0"):
    """Watchdog wired for code-drift tests: mock remote.version() → deployed_commit,
    mock event_bus/outreach. `_last_deployed_commit` (the deploy baseline drift is
    measured against) is stubbed to `deploy_ref` so the check runs without a DB;
    pass deploy_ref=None to exercise the no-baseline skip. `_host_contains_commit`
    is stubbed per-test."""
    r = AsyncMock()
    r.version = AsyncMock(return_value={"deployed_commit": deployed_commit})
    event_bus = AsyncMock()
    outreach = AsyncMock()
    wd = GuardianWatchdog(r, event_bus=event_bus, outreach_pipeline=outreach)
    wd._last_deployed_commit = AsyncMock(return_value=deploy_ref)
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
        outreach.submit_raw.assert_not_called()
        assert not any("code_drift" in e for e in _emitted_events(eb))
        wd._host_contains_commit.assert_awaited()  # the ancestry path ran

    @pytest.mark.asyncio
    async def test_genuine_drift_alerts_once_per_episode(self):
        """Host genuinely missing the Guardian commit → quiet below threshold,
        then ONE user alert per episode — NOT every 3 ticks like the event-bus
        emit. (The spam fix: ticking far past the threshold yields one Telegram.)"""
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="0ld0ld0")
        wd._host_contains_commit = AsyncMock(return_value=False)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_git_side_effect("a915a28")):
            for _ in range(_THRESHOLD - 1):
                await wd._check_code_drift_inner()
            outreach.submit_raw.assert_not_called()   # below threshold: silent
            for _ in range(2 * _THRESHOLD):           # tick well past threshold
                await wd._check_code_drift_inner()
        assert wd._drift_count >= _THRESHOLD
        assert any("code_drift" in e for e in _emitted_events(eb))  # bus re-emits
        outreach.submit_raw.assert_called_once()       # user alerted exactly once
        req = outreach.submit_raw.call_args.args[1]
        assert req.source_id == "guardian:code_drift"
        assert req.topic == "Guardian code drift"

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
        outreach.submit_raw.assert_not_called()
        eb.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_drift_then_resolved_resets_and_rearms(self):
        """A drift episode resolves (host catches up) → counter AND the user-alert
        flag reset; a SECOND episode alerts again (no 'quiet forever')."""
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="0ld0ld0")
        wd._host_contains_commit = AsyncMock(return_value=False)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_git_side_effect("a915a28")):
            for _ in range(_THRESHOLD):
                await wd._check_code_drift_inner()
            assert wd._drift_count == _THRESHOLD
            outreach.submit_raw.assert_called_once()   # episode 1: one alert
            wd._host_contains_commit = AsyncMock(return_value=True)  # host caught up
            await wd._check_code_drift_inner()
            assert wd._drift_count == 0
            assert wd._drift_escalated is False         # re-armed
            wd._host_contains_commit = AsyncMock(return_value=False)  # episode 2
            for _ in range(_THRESHOLD):
                await wd._check_code_drift_inner()
        assert outreach.submit_raw.call_count == 2      # second episode re-alerts

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
        outreach.submit_raw.assert_not_called()
        assert not any("code_drift" in e for e in _emitted_events(eb))

    @pytest.mark.asyncio
    async def test_reference_is_deploy_baseline_not_head(self):
        """The container reference is the last DEPLOYED commit, not live HEAD:
        the `git log` that derives the Guardian commit is scoped to the deploy
        baseline. This is the fix for the per-commit false alarm — container
        `main` races ahead of the host between update.sh runs, so HEAD is the
        wrong reference."""
        from unittest.mock import MagicMock
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="dep10y0",
                                              deploy_ref="dep10y0")
        wd._host_contains_commit = AsyncMock(return_value=True)
        seen: dict = {}

        def _se(cmd, **kw):
            if "symbolic-ref" in cmd:
                return MagicMock(returncode=0, stdout="main\n")
            if "log" in cmd:
                seen["log_cmd"] = cmd
                return MagicMock(returncode=0, stdout="dep10y0\n")
            return MagicMock(returncode=0, stdout="")

        with patch("genesis.guardian.watchdog.subprocess.run", side_effect=_se):
            await wd._check_code_drift_inner()
        # git log was scoped to the deploy baseline (not implicit HEAD)
        assert "dep10y0" in seen["log_cmd"]
        assert wd._drift_count == 0
        outreach.submit_raw.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_deploy_baseline_skips_no_alarm(self):
        """No successful update recorded → no baseline → skip the whole check
        (never fall back to live HEAD). version() is not even queried, so a host
        that is legitimately behind because no deploy has run yet cannot
        false-alarm — even ticking far past the threshold."""
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="0ld0ld0",
                                              deploy_ref=None)
        wd._host_contains_commit = AsyncMock(return_value=False)  # would alarm if reached
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_git_side_effect("a915a28")):
            for _ in range(_THRESHOLD + 1):
                await wd._check_code_drift_inner()
        assert wd._drift_count == 0
        r.version.assert_not_called()
        wd._host_contains_commit.assert_not_awaited()
        outreach.submit_raw.assert_not_called()
        eb.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_guardian_commit_reachable_skips(self):
        """deploy_ref resolves but `git log` finds no Guardian-path commit at/
        before it (exit 0, empty stdout) → skip before the SSH round-trip, no
        alarm. Guards the empty-container_hash branch."""
        wd, r, eb, outreach = _drift_watchdog(deployed_commit="0ld0ld0",
                                              deploy_ref="dep10y0")
        wd._host_contains_commit = AsyncMock(return_value=False)
        with patch("genesis.guardian.watchdog.subprocess.run",
                   side_effect=_git_side_effect("")):   # git log → empty
            await wd._check_code_drift_inner()
        assert wd._drift_count == 0
        r.version.assert_not_called()
        wd._host_contains_commit.assert_not_awaited()
        outreach.submit_raw.assert_not_called()


class TestLastDeployedCommit:
    """The deploy baseline read from update_history: the commit update.sh last
    SUCCESSFULLY deployed. Drift is measured against this, not container HEAD."""

    @staticmethod
    def _seed_db(path, rows):
        import sqlite3
        con = sqlite3.connect(str(path))
        con.execute(
            "CREATE TABLE update_history (id TEXT PRIMARY KEY, old_tag TEXT, "
            "new_tag TEXT, old_commit TEXT, new_commit TEXT, status TEXT, "
            "rollback_tag TEXT, failure_reason TEXT, degraded_subsystems TEXT, "
            "started_at TEXT, completed_at TEXT)",
        )
        con.executemany(
            "INSERT INTO update_history (id, new_commit, status, started_at) "
            "VALUES (?, ?, ?, ?)", rows,
        )
        con.commit()
        con.close()

    @pytest.mark.asyncio
    async def test_returns_latest_success_new_commit(self, tmp_path, monkeypatch):
        """Most recent status='success' row wins; a newer non-success row (e.g.
        a failed deploy after) is correctly ignored."""
        db = tmp_path / "genesis.db"
        self._seed_db(db, [
            ("1", "OLDcom", "success", "2026-06-23T01:00:00+00:00"),
            ("2", "NEWcom", "success", "2026-06-23T14:00:00+00:00"),
            ("3", "FAILcom", "failed", "2026-06-23T15:00:00+00:00"),
        ])
        monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        assert await wd._last_deployed_commit() == "NEWcom"

    @pytest.mark.asyncio
    async def test_no_success_rows_returns_none(self, tmp_path, monkeypatch):
        db = tmp_path / "genesis.db"
        self._seed_db(db, [
            ("1", "Xcom", "failed", "2026-06-23T01:00:00+00:00"),
            ("2", "Ycom", "rolled_back", "2026-06-23T02:00:00+00:00"),
        ])
        monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        assert await wd._last_deployed_commit() is None

    @pytest.mark.asyncio
    async def test_missing_db_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("genesis.env.genesis_db_path",
                            lambda: tmp_path / "nonexistent.db")
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        assert await wd._last_deployed_commit() is None

    @pytest.mark.asyncio
    async def test_orders_by_true_instant_not_lexicographic(self, tmp_path, monkeypatch):
        """Mixed timezone offsets must order by actual instant, not raw string.
        Values chosen so lexicographic and true-instant order DIFFER: the row
        whose string sorts higher ('...T20:00:00+05:00' = 15:00 UTC) is the
        EARLIER instant, so it must NOT win over '...T16:00:00+00:00' (16:00 UTC)."""
        db = tmp_path / "genesis.db"
        self._seed_db(db, [
            ("1", "WRONG_lexwin", "success", "2026-06-23T20:00:00+05:00"),   # 15:00 UTC
            ("2", "RIGHT_instant", "success", "2026-06-23T16:00:00+00:00"),  # 16:00 UTC
        ])
        monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)
        wd = GuardianWatchdog(AsyncMock(), event_bus=None)
        assert await wd._last_deployed_commit() == "RIGHT_instant"


class TestAlertUser:
    """Direct coverage of the _alert_user delivery helper (shared by all three
    escalation paths). Must reach the outreach pipeline, degrade gracefully when
    unwired, and never propagate an outreach failure into a watchdog tick."""

    @pytest.mark.asyncio
    async def test_submits_raw_with_blocker_envelope(self):
        from genesis.outreach.types import OutreachCategory
        outreach = AsyncMock()
        wd = GuardianWatchdog(AsyncMock(), event_bus=None, outreach_pipeline=outreach)
        await wd._alert_user(
            topic="Guardian code drift", context="msg body",
            source_id="guardian:code_drift",
        )
        outreach.submit_raw.assert_awaited_once()
        text, req = outreach.submit_raw.call_args.args
        assert text == "msg body"
        assert req.category == OutreachCategory.BLOCKER  # → Telegram, skips quiet hours
        assert req.signal_type == "guardian_alert"
        assert req.topic == "Guardian code drift"
        assert req.source_id == "guardian:code_drift"

    @pytest.mark.asyncio
    async def test_no_pipeline_is_noop(self):
        wd = GuardianWatchdog(AsyncMock(), event_bus=None, outreach_pipeline=None)
        await wd._alert_user(topic="t", context="c", source_id="guardian:x")  # no raise

    @pytest.mark.asyncio
    async def test_outreach_failure_never_propagates(self):
        outreach = AsyncMock()
        outreach.submit_raw.side_effect = Exception("telegram down")
        wd = GuardianWatchdog(AsyncMock(), event_bus=None, outreach_pipeline=outreach)
        await wd._alert_user(topic="t", context="c", source_id="guardian:x")  # swallowed


# --- Authorized-keys hardening reconciler -----------------------------------

# sha256 hexdigests of the host-observed SSH source (values arbitrary).
_SRC_A = "c" * 64
_SRC_B = "d" * 64
_SRC_C = "e" * 64


def _authkey_vi(*, no_pty=True, has_from=True, from_matches=True,
                src_hash=_SRC_A) -> dict:
    """version() payload carrying the authkey_* hardening indicators."""
    return {
        "gateway_sha": _EXPECTED_GW_SHA,
        "code_version": "abc1234",
        "authkey_no_pty": no_pty,
        "authkey_has_from": has_from,
        "authkey_from_matches": from_matches,
        "authkey_observed_src_hash": src_hash,
        "authkey_opts_hash": "f" * 64,
    }


def _ak_watchdog():
    wd, r, eb, outreach = _gw_watchdog()
    r.reharden_key = AsyncMock(
        return_value={"ok": True, "changed": True, "confirmed": True})
    return wd, r, eb, outreach


def _alert_ids(outreach) -> list[str]:
    return [c.args[1].source_id for c in outreach.submit_raw.call_args_list]


class TestAuthkeyHardening:
    """Self-heal reconciler for the host guardian authorized_keys line.

    Two trigger classes: a hardening REGRESSION (no-pty stripped, or from=
    missing while the source is derivable) heals immediately — those states
    don't oscillate. A from= MISMATCH heals only after a stable streak (same
    observed-source hash for DRIFT_ALERT_THRESHOLD consecutive ticks); a
    source that changes between ticks is a flap and must NEVER be chased —
    escalate instead. The flap guard is non-sticky: a source that later
    stabilizes reaches the streak and heals.
    """

    @pytest.mark.asyncio
    async def test_missing_fields_skips(self):
        """Old gateway without authkey_* fields → never act, never alarm."""
        wd, r, eb, outreach = _ak_watchdog()
        await wd._check_authkey_hardening(
            {"gateway_sha": _EXPECTED_GW_SHA, "code_version": "abc1234"})
        r.reharden_key.assert_not_called()
        outreach.submit_raw.assert_not_called()
        assert wd._authkey_drift_count == 0

    @pytest.mark.asyncio
    async def test_healthy_resets_state(self):
        wd, r, _, _ = _ak_watchdog()
        wd._authkey_drift_count = 2
        wd._authkey_reharden_attempted = True
        wd._authkey_escalated = True
        wd._authkey_flap_escalated = True
        wd._authkey_last_src_hash = _SRC_B
        await wd._check_authkey_hardening(_authkey_vi())
        r.reharden_key.assert_not_called()
        assert wd._authkey_drift_count == 0
        assert wd._authkey_reharden_attempted is False
        assert wd._authkey_escalated is False
        assert wd._authkey_flap_escalated is False
        assert wd._authkey_last_src_hash is None

    @pytest.mark.asyncio
    async def test_no_pty_regression_heals_immediately(self):
        """A stripped no-pty is a non-oscillating regression → heal on the
        FIRST tick, and tell the operator a reharden happened."""
        wd, r, eb, outreach = _ak_watchdog()
        await wd._check_authkey_hardening(_authkey_vi(no_pty=False))
        r.reharden_key.assert_awaited_once()
        assert any("authkey" in e for e in _emitted_events(eb))
        assert "guardian:authkey_rehardened" in _alert_ids(outreach)

    @pytest.mark.asyncio
    async def test_regression_heals_once_then_escalates_once(self):
        wd, r, _, outreach = _ak_watchdog()
        vi = _authkey_vi(no_pty=False)
        for _ in range(4):
            await wd._check_authkey_hardening(vi)
        r.reharden_key.assert_awaited_once()  # one heal per episode
        drift_alerts = [s for s in _alert_ids(outreach)
                        if s == "guardian:authkey_drift"]
        assert len(drift_alerts) == 1  # escalated exactly once

    @pytest.mark.asyncio
    async def test_missing_from_with_src_heals(self):
        wd, r, _, _ = _ak_watchdog()
        await wd._check_authkey_hardening(
            _authkey_vi(has_from=False, from_matches=False))
        r.reharden_key.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_from_without_src_never_heals(self):
        """No observable source → a reharden could not add from= anyway;
        do nothing rather than churn (fail-safe)."""
        wd, r, _, outreach = _ak_watchdog()
        vi = _authkey_vi(has_from=False, from_matches=False, src_hash="")
        for _ in range(_THRESHOLD + 1):
            await wd._check_authkey_hardening(vi)
        r.reharden_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_from_mismatch_heals_only_at_stable_streak(self):
        """A moved-but-stable source heals exactly once, and only after
        DRIFT_ALERT_THRESHOLD consecutive ticks with the SAME source hash."""
        wd, r, _, _ = _ak_watchdog()
        vi = _authkey_vi(from_matches=False, src_hash=_SRC_B)
        for _ in range(_THRESHOLD - 1):
            await wd._check_authkey_hardening(vi)
        r.reharden_key.assert_not_called()
        await wd._check_authkey_hardening(vi)  # threshold tick
        r.reharden_key.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_from_mismatch_empty_src_never_heals(self):
        """Mismatch reported but the source is unobservable → no streak can
        form; never reharden on it."""
        wd, r, _, _ = _ak_watchdog()
        vi = _authkey_vi(from_matches=False, src_hash="")
        for _ in range(_THRESHOLD + 1):
            await wd._check_authkey_hardening(vi)
        r.reharden_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_flap_never_heals_escalates_once(self):
        """Source hash differing between mismatch ticks = flapping network —
        rewriting from= each tick would churn the key file forever. Never
        heal; escalate exactly once with the real remedy."""
        wd, r, _, outreach = _ak_watchdog()
        for h in (_SRC_A, _SRC_B, _SRC_C, _SRC_A, _SRC_B):
            await wd._check_authkey_hardening(
                _authkey_vi(from_matches=False, src_hash=h))
        r.reharden_key.assert_not_called()
        flap_alerts = [s for s in _alert_ids(outreach)
                       if s == "guardian:authkey_flap"]
        assert len(flap_alerts) == 1

    @pytest.mark.asyncio
    async def test_flap_then_stable_source_recovers(self):
        """The flap guard is NON-STICKY: once the source stabilizes for a
        full streak, the reconciler heals — no wedged state needing a
        restart."""
        wd, r, _, _ = _ak_watchdog()
        await wd._check_authkey_hardening(
            _authkey_vi(from_matches=False, src_hash=_SRC_A))
        for _ in range(_THRESHOLD):  # stabilizes on B
            await wd._check_authkey_hardening(
                _authkey_vi(from_matches=False, src_hash=_SRC_B))
        r.reharden_key.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolved_resets_and_rearms(self):
        """After a healed episode resolves, a NEW episode must self-heal
        again (no 'quiet forever')."""
        wd, r, _, _ = _ak_watchdog()
        for _ in range(_THRESHOLD):  # episode 1: stable move → heal
            await wd._check_authkey_hardening(
                _authkey_vi(from_matches=False, src_hash=_SRC_B))
        assert r.reharden_key.await_count == 1
        await wd._check_authkey_hardening(_authkey_vi())  # resolved → reset
        for _ in range(_THRESHOLD):  # episode 2 → re-armed
            await wd._check_authkey_hardening(
                _authkey_vi(from_matches=False, src_hash=_SRC_C))
        assert r.reharden_key.await_count == 2
