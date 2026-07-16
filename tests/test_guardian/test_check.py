"""Tests for Guardian main check logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.check import (
    _build_dispatcher,
    _build_provisioning_adapter,
    _check_storage_pool_and_alert,
    _execute_recovery_with_approval,
    _handle_cc_resolved,
    _handle_confirmed_dead,
    _handle_healthy,
    _maintain_snapshots,
    _write_guardian_heartbeat,
    run_check,
)
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisEngine, DiagnosisResult, RecoveryAction
from genesis.guardian.health_signals import HealthSnapshot, PauseState, SignalResult
from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState


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
                "container_exists",
                "icmp_reachable",
                "health_api",
                "heartbeat_canary",
                "log_freshness",
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
    async def test_does_not_write_heartbeat_directly(self, config: GuardianConfig) -> None:
        """_handle_healthy must NOT call the heartbeat — run_check owns it."""
        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=0)

        with patch(
            "genesis.guardian.check._write_guardian_heartbeat",
            AsyncMock(),
        ) as mock_hb:
            await _handle_healthy(config, snapshots)
        mock_hb.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_healthy_no_longer_prunes(self, config: GuardianConfig) -> None:
        """Prune moved to _maintain_snapshots (runs for ALL states)."""
        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=0)
        snapshots.enforce_expiry_policy = AsyncMock(return_value=True)

        await _handle_healthy(config, snapshots)
        snapshots.prune.assert_not_called()


class TestMaintainSnapshots:
    @pytest.mark.asyncio
    async def test_prunes_when_due(self, config: GuardianConfig) -> None:
        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=2)
        snapshots.enforce_expiry_policy = AsyncMock(return_value=True)

        await _maintain_snapshots(config, snapshots)
        # No marker file exists = overdue → prune runs; expiry always enforced.
        snapshots.prune.assert_called_once()
        snapshots.enforce_expiry_policy.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_all_work_when_not_due(
        self,
        config: GuardianConfig,
    ) -> None:
        """Within the 24h throttle window, neither expiry nor prune runs — the
        marker gate avoids a per-tick incus subprocess."""
        from datetime import UTC, datetime

        marker = config.state_path / ".last_prune"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(UTC).isoformat())  # fresh → not due

        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=0)
        snapshots.enforce_expiry_policy = AsyncMock(return_value=True)

        await _maintain_snapshots(config, snapshots)
        snapshots.enforce_expiry_policy.assert_not_called()
        snapshots.prune.assert_not_called()

    @pytest.mark.asyncio
    async def test_prune_failure_does_not_stop_marker_write(
        self,
        config: GuardianConfig,
    ) -> None:
        """A prune exception is swallowed and the marker still advances (no
        tight-loop retry storm on a persistently failing incus)."""
        snapshots = MagicMock()
        snapshots.enforce_expiry_policy = AsyncMock(return_value=True)
        snapshots.prune = AsyncMock(side_effect=RuntimeError("incus down"))

        await _maintain_snapshots(config, snapshots)
        assert (config.state_path / ".last_prune").exists()


class TestStoragePoolAlert:
    @pytest.mark.asyncio
    async def test_crit_alerts_and_persists_state(self, config: GuardianConfig) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=True)
        status = StoragePoolStatus(detected=True, data_pct=95.0, metadata_pct=30.0)

        with patch("genesis.guardian.check.measure_storage_pool", AsyncMock(return_value=status)):
            await _check_storage_pool_and_alert(config, dispatcher)

        dispatcher.send.assert_called_once()
        alert = dispatcher.send.call_args.args[0]
        assert alert.severity.value == "critical"
        assert (config.state_path / "pool_alert_state.json").exists()

    @pytest.mark.asyncio
    async def test_sustained_tier_does_not_double_alert(self, config: GuardianConfig) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=True)
        status = StoragePoolStatus(detected=True, data_pct=95.0)

        with patch("genesis.guardian.check.measure_storage_pool", AsyncMock(return_value=status)):
            await _check_storage_pool_and_alert(config, dispatcher)  # first: alerts
            await _check_storage_pool_and_alert(config, dispatcher)  # same tier, within interval
        # Only the first tick alerts; the second is suppressed by hysteresis.
        assert dispatcher.send.call_count == 1

    @pytest.mark.asyncio
    async def test_undetected_pool_never_alerts(self, config: GuardianConfig) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=True)
        status = StoragePoolStatus(detected=False, detail="not lvm")

        with patch("genesis.guardian.check.measure_storage_pool", AsyncMock(return_value=status)):
            await _check_storage_pool_and_alert(config, dispatcher)
        dispatcher.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_short_circuits(self, config: GuardianConfig) -> None:
        config.storage_pool.enabled = False
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=True)
        called = MagicMock()
        with patch("genesis.guardian.check.measure_storage_pool", called):
            await _check_storage_pool_and_alert(config, dispatcher)
        called.assert_not_called()


def _prov_cfg(config: GuardianConfig) -> GuardianConfig:
    config.provisioning.enabled = True
    config.provisioning.api_host = "10.0.0.9"
    config.provisioning.node = "pve"
    config.provisioning.vmid = 100
    return config


class TestBuildProvisioningAdapter:
    def test_disabled_returns_none(self, config: GuardianConfig) -> None:
        assert _build_provisioning_adapter(config) is None

    def test_enabled_but_unconfigured_returns_none(self, config: GuardianConfig) -> None:
        config.provisioning.enabled = True  # api_host/node/vmid still unset
        assert _build_provisioning_adapter(config) is None

    def test_env_tokens_build_adapter(
        self,
        config: GuardianConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _prov_cfg(config)
        monkeypatch.setenv("PROXMOX_AUDIT_TOKEN", "aud-tok")
        monkeypatch.setenv("PROXMOX_PROVISION_TOKEN", "prov-tok")
        monkeypatch.setenv("PROXMOX_BACKUP_TOKEN", "bak-tok")
        adapter = _build_provisioning_adapter(config)
        assert adapter is not None
        assert adapter._audit == "aud-tok"
        assert adapter._provision == "prov-tok"
        assert adapter._backup == "bak-tok"

    def test_missing_backup_token_degrades_not_fails(
        self,
        config: GuardianConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No backup token → adapter still builds; only backup verbs refuse."""
        _prov_cfg(config)
        monkeypatch.setenv("PROXMOX_AUDIT_TOKEN", "aud-tok")
        monkeypatch.setenv("PROXMOX_PROVISION_TOKEN", "prov-tok")
        monkeypatch.delenv("PROXMOX_BACKUP_TOKEN", raising=False)
        adapter = _build_provisioning_adapter(config)
        assert adapter is not None
        assert adapter._backup == ""

    def test_audit_only_is_readonly_adapter(
        self,
        config: GuardianConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _prov_cfg(config)
        monkeypatch.setenv("PROXMOX_AUDIT_TOKEN", "aud-tok")
        monkeypatch.delenv("PROXMOX_PROVISION_TOKEN", raising=False)
        # Isolate from a real host secrets.env that might carry a provision token.
        with patch("genesis.guardian.check.load_secrets", return_value={}):
            adapter = _build_provisioning_adapter(config)
        assert adapter is not None
        assert adapter._audit == "aud-tok"
        assert adapter._provision == ""

    def test_no_tokens_anywhere_returns_none(
        self,
        config: GuardianConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _prov_cfg(config)
        monkeypatch.delenv("PROXMOX_AUDIT_TOKEN", raising=False)
        monkeypatch.delenv("PROXMOX_PROVISION_TOKEN", raising=False)
        with patch("genesis.guardian.check.load_secrets", return_value={}):
            assert _build_provisioning_adapter(config) is None


class TestProvisioningProposeHook:
    """The autonomous propose only fires on a CRITICAL pool AND Genesis down."""

    @pytest.mark.asyncio
    async def test_crit_and_genesis_down_proposes(self, config: GuardianConfig) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=True)
        status = StoragePoolStatus(detected=True, data_pct=95.0)
        with (
            patch("genesis.guardian.check.measure_storage_pool", AsyncMock(return_value=status)),
            patch("genesis.guardian.check._maybe_propose_provisioning", AsyncMock()) as propose,
        ):
            await _check_storage_pool_and_alert(config, dispatcher, genesis_down=True)
        propose.assert_called_once()

    @pytest.mark.asyncio
    async def test_crit_but_genesis_up_does_not_propose(self, config: GuardianConfig) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=True)
        status = StoragePoolStatus(detected=True, data_pct=95.0)
        with (
            patch("genesis.guardian.check.measure_storage_pool", AsyncMock(return_value=status)),
            patch("genesis.guardian.check._maybe_propose_provisioning", AsyncMock()) as propose,
        ):
            await _check_storage_pool_and_alert(config, dispatcher, genesis_down=False)
        propose.assert_not_called()

    @pytest.mark.asyncio
    async def test_warn_tier_never_proposes_even_if_down(self, config: GuardianConfig) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=True)
        status = StoragePoolStatus(detected=True, data_pct=80.0)  # warn, not crit
        with (
            patch("genesis.guardian.check.measure_storage_pool", AsyncMock(return_value=status)),
            patch("genesis.guardian.check._maybe_propose_provisioning", AsyncMock()) as propose,
        ):
            await _check_storage_pool_and_alert(config, dispatcher, genesis_down=True)
        propose.assert_not_called()


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
    async def test_writes_heartbeat_on_healthy(self, config: GuardianConfig) -> None:
        """Successful HEALTHY check cycle writes heartbeat once."""
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch(
                "genesis.guardian.check._write_guardian_heartbeat",
                AsyncMock(),
            ) as mock_hb,
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)
        mock_hb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_writes_heartbeat_on_signal_dropped(self, config: GuardianConfig) -> None:
        """SIGNAL_DROPPED is still a successful cycle — heartbeat fires."""
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
            patch(
                "genesis.guardian.check._write_guardian_heartbeat",
                AsyncMock(),
            ) as mock_hb,
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)
        mock_hb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_write_heartbeat_on_crash(self, config: GuardianConfig) -> None:
        """If _check_cycle raises, heartbeat must NOT fire — Guardian failure
        should be visible to Genesis-side monitoring."""
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "genesis.guardian.check._write_guardian_heartbeat",
                AsyncMock(),
            ) as mock_hb,
            patch("genesis.guardian.check.load_secrets", return_value={}),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await run_check(config)
        mock_hb.assert_not_called()

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


def _dead_snapshot() -> HealthSnapshot:
    """Container/probe failure — not all_alive."""
    return HealthSnapshot(
        signals={
            "container_exists": SignalResult("container_exists", False, 1.0, "down", "t"),
            "icmp_reachable": SignalResult("icmp_reachable", False, 1.0, "down", "t"),
            "health_api": SignalResult("health_api", False, 1.0, "down", "t"),
            "heartbeat_canary": SignalResult("heartbeat_canary", True, 1.0, "ok", "t"),
            "log_freshness": SignalResult("log_freshness", True, 1.0, "ok", "t"),
        },
        pause_state=PauseState(paused=False),
    )


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _cc_unavailable_diagnosis() -> DiagnosisResult:
    """The escalate/cc-unavailable diagnosis that storms today."""
    return DiagnosisResult(
        likely_cause="CC diagnosis unavailable — cannot determine root cause safely",
        confidence_pct=0,
        evidence=[],
        recommended_action=RecoveryAction.ESCALATE,
        reasoning="CC unavailable",
        source="cc_unavailable",
    )


def _resolved_diagnosis() -> DiagnosisResult:
    """CC reports it already fixed the issue (outcome=resolved)."""
    return DiagnosisResult(
        likely_cause="transient blip",
        confidence_pct=90,
        evidence=[],
        recommended_action=RecoveryAction.RESTART_SERVICES,
        reasoning="CC self-resolved",
        source="cc",
        outcome="resolved",
        actions_taken=["restart_services"],
    )


def _proposed_diagnosis(
    action: RecoveryAction = RecoveryAction.RESTART_SERVICES,
) -> DiagnosisResult:
    """Propose-only diagnosis: CC investigated and PROPOSES an action (never acts)."""
    return DiagnosisResult(
        likely_cause="server process crashed",
        confidence_pct=80,
        evidence=[],
        recommended_action=action,
        reasoning="proposed for approval",
        source="cc",
        outcome="proposed",
        actions_taken=[],
    )


def _seed_state(config: GuardianConfig, **fields) -> Path:
    """Write a minimal state.json so run_check loads it (from_dict fills defaults)."""
    import json

    config.state_path.mkdir(parents=True, exist_ok=True)
    state_path = config.state_path / "state.json"
    state_path.write_text(json.dumps(fields))
    return state_path


def _alert_titles(mock_dispatcher: MagicMock) -> list[str]:
    """Titles of every Alert passed to dispatcher.send (Alert is the positional arg)."""
    return [call.args[0].title for call in mock_dispatcher.send.call_args_list]


class TestGuardianAlertOnce:
    """GUARD-R2-01 — alert once per down-episode + a single 'restored' ping.

    Minimal scope: stop the every-30s storm (re-diagnosis + re-alert) and add the
    currently-missing restored notification for autonomous recovery.
    """

    @pytest.mark.asyncio
    async def test_handle_confirmed_dead_skips_when_down_alert_already_sent(
        self, config: GuardianConfig
    ) -> None:
        """Repeat ticks must NOT re-diagnose or re-alert once the episode was handled."""
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = True

        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        diagnosis_engine = MagicMock()
        diagnosis_engine.diagnose = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        await _handle_confirmed_dead(config, sm, dispatcher, diagnosis_engine, recovery_engine)

        diagnosis_engine.diagnose.assert_not_called()  # no expensive Opus re-run
        dispatcher.send.assert_not_called()  # no re-alert
        recovery_engine.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_confirmed_dead_proceeds_and_marks_flag_when_clear(
        self, config: GuardianConfig
    ) -> None:
        """First handling (flag clear) diagnoses, then marks the episode as alerted."""
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = False
        sm._state.first_failure_at = _now_iso()

        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        diagnosis_engine = MagicMock()
        diagnosis_engine.diagnose = AsyncMock(return_value=_cc_unavailable_diagnosis())
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
        ):
            await _handle_confirmed_dead(config, sm, dispatcher, diagnosis_engine, recovery_engine)

        diagnosis_engine.diagnose.assert_awaited_once()
        assert sm.state.down_alert_sent is True  # episode now marked — next tick skips

    @pytest.mark.asyncio
    async def test_autonomous_recovery_sends_one_restored_ping_and_clears_flag(
        self, config: GuardianConfig
    ) -> None:
        """Container returns on its own from CONFIRMED_DEAD → exactly one restored ping."""
        state_path = _seed_state(
            config,
            current_state="confirmed_dead",
            down_alert_sent=True,
            first_failure_at=_now_iso(),
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.send = AsyncMock()

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._handle_healthy", AsyncMock()),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        titles = _alert_titles(mock_dispatcher)
        restored = [t for t in titles if "restored" in t.lower() or "recovered" in t.lower()]
        assert len(restored) == 1, f"expected one restored ping, got titles={titles}"

        import json

        saved = json.loads(state_path.read_text())
        assert saved["current_state"] == "healthy"
        assert saved["down_alert_sent"] is False  # cleared on genuine recovery

    @pytest.mark.asyncio
    async def test_auto_reset_does_not_ping_or_clear_flag(self, config: GuardianConfig) -> None:
        """Auto-reset (still-down) must NOT emit a restored ping nor clear the flag.

        The auto-reset trap: auto-reset routes CONFIRMED_DEAD→HEALTHY while the
        container is STILL down (all_alive=False). A naive 'clear on recovery'
        would wipe the throttle here and re-enable the storm.
        """
        state_path = _seed_state(
            config,
            current_state="confirmed_dead",
            down_alert_sent=True,
            first_failure_at="2026-01-01T00:00:00+00:00",  # long past timeout → auto-reset
            auto_reset_count=0,
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.send = AsyncMock()

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._handle_healthy", AsyncMock()),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        titles = _alert_titles(mock_dispatcher)
        assert not any("restored" in t.lower() or "recovered" in t.lower() for t in titles), (
            f"auto-reset must not ping restored; titles={titles}"
        )

        import json

        saved = json.loads(state_path.read_text())
        assert saved["down_alert_sent"] is True  # retained — storm stays suppressed

    @pytest.mark.asyncio
    async def test_storm_suppressed_across_ticks(self, config: GuardianConfig) -> None:
        """Two CONFIRMED_DEAD ticks: alert on the first, silence on the second."""
        _seed_state(
            config,
            current_state="confirmed_dead",
            down_alert_sent=False,
            first_failure_at=_now_iso(),  # recent → no auto-reset
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.send = AsyncMock()

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine,
                "diagnose",
                AsyncMock(return_value=_cc_unavailable_diagnosis()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)  # tick 1 — should alert
            count_after_tick1 = mock_dispatcher.send.call_count
            await run_check(config)  # tick 2 — should stay quiet
            count_after_tick2 = mock_dispatcher.send.call_count

        assert count_after_tick1 >= 1, "first down tick must alert"
        assert count_after_tick2 == count_after_tick1, (
            "second tick must NOT re-alert (storm killed)"
        )

    @pytest.mark.asyncio
    async def test_diagnosis_crash_unmarks_flag_for_retry(self, config: GuardianConfig) -> None:
        """If diagnosis raises before any alert, the flag is un-marked so the
        next tick retries instead of silently muting the episode."""
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = False

        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        diagnosis_engine = MagicMock()
        diagnosis_engine.diagnose = AsyncMock(side_effect=RuntimeError("cc crashed"))
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            pytest.raises(RuntimeError, match="cc crashed"),
        ):
            await _handle_confirmed_dead(config, sm, dispatcher, diagnosis_engine, recovery_engine)

        assert sm.state.down_alert_sent is False  # un-marked → next tick retries

    @pytest.mark.asyncio
    async def test_cc_resolved_is_firewalled_not_auto_confirmed(
        self, config: GuardianConfig
    ) -> None:
        """Propose-only firewall: a CC 'resolved' claim is NOT trusted.

        Under propose-only mode CC must never self-resolve. If it does anyway
        (a regression / stale response), `_handle_cc_resolved` must route to the
        approval gate and never auto-confirm recovery. With no Telegram channel
        (MagicMock dispatcher → empty `_channels`), the gate falls back to a
        manual-intervention alert and does NOT auto-recover: state stays
        CONFIRMED_DEAD and the storm-suppression flag is retained (it clears only
        on genuine recovery — covered by the autonomous-recovery test above)."""
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = True

        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with patch(
            "genesis.guardian.check.collect_all_signals",
            AsyncMock(return_value=_dead_snapshot()),
        ):
            await _handle_cc_resolved(
                config, sm, dispatcher, _resolved_diagnosis(), recovery_engine
            )

        # The untrusted "resolved" claim must NOT trigger auto-recovery...
        recovery_engine.execute.assert_not_called()
        # ...nor flip the system healthy on CC's say-so...
        assert sm.current_state == GuardianState.CONFIRMED_DEAD
        # ...and the episode stays open (storm stays suppressed; clears on real recovery).
        assert sm.state.down_alert_sent is True
        # A manual-intervention alert must have fired (no Telegram approval channel).
        titles = _alert_titles(dispatcher)
        assert any(
            "approval gate unavailable" in t.lower() or "no telegram" in t.lower() for t in titles
        ), f"expected manual-intervention alert; got {titles}"

    @pytest.mark.asyncio
    async def test_full_episode_is_two_pings_regardless_of_duration(
        self, config: GuardianConfig
    ) -> None:
        """Acceptance (user requirement): one 'down' + one 'restored' per episode,
        no matter how many ticks it stays down."""
        _seed_state(
            config,
            current_state="confirmed_dead",
            down_alert_sent=False,
            first_failure_at=_now_iso(),
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.send = AsyncMock()

        # Three consecutive down ticks (CC unavailable).
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine,
                "diagnose",
                AsyncMock(return_value=_cc_unavailable_diagnosis()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)
            down_side_alerts = mock_dispatcher.send.call_count
            await run_check(config)
            await run_check(config)

        # Down-side alert count did NOT grow over the extra ticks.
        assert mock_dispatcher.send.call_count == down_side_alerts
        assert down_side_alerts >= 1

        # Now it recovers on its own.
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._handle_healthy", AsyncMock()),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        titles = _alert_titles(mock_dispatcher)
        restored = [t for t in titles if "restored" in t.lower() or "recovered" in t.lower()]
        assert len(restored) == 1, f"expected exactly one restored ping; titles={titles}"


def _mock_gate_channel(poll_returns: object) -> MagicMock:
    """Mock TelegramAlertChannel for the approval gate.

    `send_text` returns a truthy message_id (the poll target is mocked, so the
    exact id is irrelevant). `poll_for_keyword` yields `poll_returns` — a single
    str (every gate gets it) or a list (one value consumed per gate).
    """
    channel = MagicMock()
    channel.send_text = AsyncMock(return_value=1)
    if isinstance(poll_returns, (list, tuple)):
        channel.poll_for_keyword = AsyncMock(side_effect=list(poll_returns))
    else:
        channel.poll_for_keyword = AsyncMock(return_value=poll_returns)
    return channel


class TestTwoGateApproval:
    """`_execute_recovery_with_approval` — gate everything: no recovery without an
    APPROVE reply to BOTH gates; self-cancel on genuine recovery; no fixed timeout."""

    def _sm(self, config: GuardianConfig) -> ConfirmationStateMachine:
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = True
        sm._state.first_failure_at = _now_iso()
        return sm

    @pytest.mark.asyncio
    async def test_approve_then_approve_executes(self, config: GuardianConfig) -> None:
        """APPROVE at both gates → the proposed action runs exactly once."""
        sm = self._sm(config)
        channel = _mock_gate_channel(["APPROVE", "APPROVE"])
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine, "diagnose", AsyncMock(return_value=_proposed_diagnosis())
            ),
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(),
            )

        recovery_engine.execute.assert_awaited_once()
        assert channel.poll_for_keyword.await_count == 2  # one reply per gate
        # _execute_recovery_with_approval delegates flag-clearing to the recovery
        # engine (which clears only on VERIFIED recovery). On APPROVE it must NOT
        # clear the flag itself — recovery_engine is mocked here, so it stays set.
        assert sm.state.down_alert_sent is True

    @pytest.mark.asyncio
    async def test_revert_redirected_to_rollback_when_git_unhealthy(
        self,
        config: GuardianConfig,
    ) -> None:
        """F.1: diagnosis proposes REVERT_CODE but container git is unhealthy → the
        PROPOSAL is redirected to SNAPSHOT_ROLLBACK BEFORE Gate 2, so the user
        approves (and the engine executes) the action that actually runs — never a
        silent post-approval action swap."""
        sm = self._sm(config)
        channel = _mock_gate_channel(["APPROVE", "APPROVE"])
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine,
                "diagnose",
                AsyncMock(return_value=_proposed_diagnosis(RecoveryAction.REVERT_CODE)),
            ),
            patch(
                "genesis.guardian.git_watch.container_git_supports_revert",
                AsyncMock(return_value=False),
            ),
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(RecoveryAction.REVERT_CODE),
            )

        recovery_engine.execute.assert_awaited_once()
        executed = recovery_engine.execute.await_args.args[0]
        assert executed.recommended_action == RecoveryAction.SNAPSHOT_ROLLBACK
        # The Gate-2 approval prompt must name the redirected action, not the revert.
        all_sent = " ".join(str(c.args[0]) for c in channel.send_text.await_args_list)
        assert "SNAPSHOT_ROLLBACK" in all_sent
        assert "REVERT_CODE" not in all_sent

    @pytest.mark.asyncio
    async def test_revert_preserved_when_git_healthy(
        self,
        config: GuardianConfig,
    ) -> None:
        """REVERT_CODE proposal + healthy container git → executed as REVERT_CODE
        (no redirect); the user approves exactly what runs."""
        sm = self._sm(config)
        channel = _mock_gate_channel(["APPROVE", "APPROVE"])
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine,
                "diagnose",
                AsyncMock(return_value=_proposed_diagnosis(RecoveryAction.REVERT_CODE)),
            ),
            patch(
                "genesis.guardian.git_watch.container_git_supports_revert",
                AsyncMock(return_value=True),
            ),
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(RecoveryAction.REVERT_CODE),
            )

        executed = recovery_engine.execute.await_args.args[0]
        assert executed.recommended_action == RecoveryAction.REVERT_CODE

    @pytest.mark.asyncio
    async def test_deny_at_gate1_does_not_execute(self, config: GuardianConfig) -> None:
        """DENY at Gate 1 → never diagnose, never execute, episode flag cleared."""
        sm = self._sm(config)
        channel = _mock_gate_channel("DENY")
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch.object(DiagnosisEngine, "diagnose", AsyncMock()) as diag,
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(),
            )

        recovery_engine.execute.assert_not_called()
        diag.assert_not_called()  # DENY at gate 1 → no diagnosis run
        assert sm.state.down_alert_sent is False  # episode closed → next tick re-diagnoses

    @pytest.mark.asyncio
    async def test_deny_at_gate2_does_not_execute(self, config: GuardianConfig) -> None:
        """APPROVE Gate 1 (diagnose) then DENY Gate 2 → action NOT executed."""
        sm = self._sm(config)
        channel = _mock_gate_channel(["APPROVE", "DENY"])
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine, "diagnose", AsyncMock(return_value=_proposed_diagnosis())
            ),
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(),
            )

        recovery_engine.execute.assert_not_called()
        assert sm.state.down_alert_sent is False

    @pytest.mark.asyncio
    async def test_self_recovery_stands_down_without_executing(
        self,
        config: GuardianConfig,
    ) -> None:
        """Health returns while waiting at Gate 1 → stand down: no poll, no
        execution, flag cleared (the no-timeout self-cancel)."""
        sm = self._sm(config)
        channel = _mock_gate_channel("APPROVE")  # would approve, but never polled
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(),
            )

        recovery_engine.execute.assert_not_called()
        channel.poll_for_keyword.assert_not_awaited()  # recovered before reading a reply
        assert sm.state.down_alert_sent is False

    @pytest.mark.asyncio
    async def test_escalate_executes_without_a_gate(self, config: GuardianConfig) -> None:
        """ESCALATE = no safe automated action → execute (manual alert) with NO
        approval gate (nothing to approve)."""
        sm = self._sm(config)
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with patch("genesis.guardian.check._find_telegram_channel") as find:
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(RecoveryAction.ESCALATE),
            )

        recovery_engine.execute.assert_awaited_once()
        find.assert_not_called()  # ESCALATE short-circuits before the gate

    @pytest.mark.asyncio
    async def test_no_telegram_channel_never_auto_recovers(
        self,
        config: GuardianConfig,
    ) -> None:
        """No Telegram channel → alert for manual action, NEVER auto-recover."""
        sm = self._sm(config)
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=None),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(),
            )

        recovery_engine.execute.assert_not_called()
        titles = _alert_titles(dispatcher)
        assert any(
            "approval gate unavailable" in t.lower() or "no telegram" in t.lower() for t in titles
        ), f"expected manual-intervention alert; got {titles}"

    @pytest.mark.asyncio
    async def test_gate1_send_failure_clears_flag_for_retry(
        self,
        config: GuardianConfig,
    ) -> None:
        """A failed Gate-1 prompt send must NOT permanently mute the Guardian: a
        failed delivery isn't a successful 'alert once', so the flag is cleared and
        the next cycle retries (recovers from a transient Telegram failure)."""
        sm = self._sm(config)
        channel = MagicMock()
        channel.send_text = AsyncMock(return_value=None)  # send fails
        channel.poll_for_keyword = AsyncMock()
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(),
            )

        recovery_engine.execute.assert_not_called()
        channel.poll_for_keyword.assert_not_awaited()  # never reached the poll
        assert sm.state.down_alert_sent is False  # cleared → next cycle retries

    @pytest.mark.asyncio
    async def test_gate2_send_failure_clears_flag_for_retry(
        self,
        config: GuardianConfig,
    ) -> None:
        """APPROVE Gate 1, then the Gate-2 prompt send fails → no execution, and
        the flag is cleared so the next cycle retries instead of going silent."""
        sm = self._sm(config)
        channel = MagicMock()
        # gate1 prompt ok, "Diagnosing…" ok, gate2 prompt FAILS (None)
        channel.send_text = AsyncMock(side_effect=[1, 111, None])
        channel.poll_for_keyword = AsyncMock(return_value="APPROVE")
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine, "diagnose", AsyncMock(return_value=_proposed_diagnosis())
            ),
        ):
            await _execute_recovery_with_approval(
                config,
                sm,
                dispatcher,
                recovery_engine,
                _proposed_diagnosis(),
            )

        recovery_engine.execute.assert_not_called()
        assert sm.state.down_alert_sent is False


class TestHealthySnapshotWiring:
    """_maintain_snapshots must produce the offline lifeline: a daily healthy
    snapshot, taken ONLY when the guardian state is HEALTHY this tick.

    Before this wiring, mark_healthy() had zero callers — SNAPSHOT_ROLLBACK
    could never succeed because nothing ever created a healthy snapshot.
    """

    def _snapshots_mock(self) -> MagicMock:
        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=0)
        snapshots.enforce_expiry_policy = AsyncMock(return_value=True)
        snapshots.mark_healthy = AsyncMock(
            return_value="guardian-20260703-000000-healthy",
        )
        return snapshots

    @pytest.mark.asyncio
    async def test_takes_healthy_snapshot_when_healthy(
        self,
        config: GuardianConfig,
    ) -> None:
        snapshots = self._snapshots_mock()
        await _maintain_snapshots(config, snapshots, is_healthy=True)
        snapshots.mark_healthy.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_healthy_snapshot_when_not_healthy(
        self,
        config: GuardianConfig,
    ) -> None:
        """NEVER snapshot a broken container as 'healthy'."""
        snapshots = self._snapshots_mock()
        await _maintain_snapshots(config, snapshots, is_healthy=False)
        snapshots.mark_healthy.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_healthy_snapshot_when_disabled(
        self,
        config: GuardianConfig,
    ) -> None:
        config.snapshots.healthy_enabled = False
        snapshots = self._snapshots_mock()
        await _maintain_snapshots(config, snapshots, is_healthy=True)
        snapshots.mark_healthy.assert_not_called()

    @pytest.mark.asyncio
    async def test_healthy_snapshot_respects_daily_marker(
        self,
        config: GuardianConfig,
    ) -> None:
        from datetime import UTC, datetime

        marker = config.state_path / ".last_prune"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(UTC).isoformat())

        snapshots = self._snapshots_mock()
        await _maintain_snapshots(config, snapshots, is_healthy=True)
        snapshots.mark_healthy.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_healthy_failure_does_not_stop_marker(
        self,
        config: GuardianConfig,
    ) -> None:
        """A failing healthy-take must not wedge the daily cadence."""
        snapshots = self._snapshots_mock()
        snapshots.mark_healthy = AsyncMock(side_effect=RuntimeError("incus down"))
        await _maintain_snapshots(config, snapshots, is_healthy=True)
        assert (config.state_path / ".last_prune").exists()
