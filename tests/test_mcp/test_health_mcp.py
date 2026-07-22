"""Tests for health-mcp server — verify all tools are registered."""

from unittest.mock import AsyncMock, patch

import genesis.mcp.health_mcp as health_mcp_mod
from genesis.mcp.health_mcp import (
    _impl_health_alerts,
    _impl_health_status,
    _impl_session_set_effort,
    _impl_session_set_model,
    mcp,
)


async def test_all_tools_registered():
    tools = await mcp.get_tools()
    for name in [
        "health_status", "health_errors", "health_alerts",
        "session_config",
    ]:
        assert name in tools, f"Missing tool: {name}"


async def test_health_status_returns_dict_without_service():
    """Without a service wired, tools return graceful unavailable response."""
    tools = await mcp.get_tools()
    result = await tools["health_status"].fn()
    assert result["status"] == "unavailable"


async def test_health_status_includes_awareness():
    """health_status should include awareness section from snapshot."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {},
        "infrastructure": {},
        "queues": {},
        "cost": {},
        "surplus": {},
        "awareness": {"status": "healthy", "ticks_24h": 42},
        "outreach_stats": {"window": "7d"},
    }
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        result = await _impl_health_status()
        assert "awareness" in result
        assert result["awareness"]["ticks_24h"] == 42
        assert "outreach_stats" in result
    finally:
        health_mcp_mod._service = old


async def test_health_status_includes_provider_activity():
    """When activity tracker is wired, health_status includes provider_activity."""
    from genesis.observability.provider_activity import ProviderActivityTracker

    tracker = ProviderActivityTracker()
    tracker.record("ollama_embedding", latency_ms=50, success=True)

    mock_service = AsyncMock()
    mock_service.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {},
        "infrastructure": {},
        "queues": {},
        "cost": {},
        "surplus": {},
    }

    old_service = health_mcp_mod._service
    old_tracker = health_mcp_mod._activity_tracker
    try:
        health_mcp_mod._service = mock_service
        health_mcp_mod._activity_tracker = tracker

        result = await _impl_health_status()
        assert "provider_activity" in result
        assert isinstance(result["provider_activity"], list)
        assert len(result["provider_activity"]) == 1
        assert result["provider_activity"][0]["provider"] == "ollama_embedding"
        assert result["provider_activity"][0]["calls"] == 1
    finally:
        health_mcp_mod._service = old_service
        health_mcp_mod._activity_tracker = old_tracker


async def test_alert_fires_on_tick_overdue():
    """Awareness tick overdue >360s should fire CRITICAL alert."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {},
        "awareness": {"time_since_last_tick_seconds": 900},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        tick_alerts = [a for a in alerts if a["id"] == "awareness:tick_overdue"]
        assert len(tick_alerts) == 1
        assert tick_alerts[0]["severity"] == "CRITICAL"
        assert "overdue" in tick_alerts[0]["message"].lower()
        assert ">360s" in tick_alerts[0]["message"]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_fires_on_stale_dead_letters():
    """Dead letters older than 1h should fire alert."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {"dead_letter_oldest_age_seconds": 7200},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        dl_alerts = [a for a in alerts if a["id"] == "queue:stale_dead_letters"]
        assert len(dl_alerts) == 1
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_call_site_alert_fires_on_down():
    """A wired call site in DOWN status should emit a WARNING alert.

    DOWN = all provider circuit breakers open (transient, self-resolving).
    Severity is WARNING (Tier 3 / reflexes only), not CRITICAL (Tier 2).
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {
            "3_micro_reflection": {  # wired in _call_site_meta
                "status": "down",
                "active_provider": None,
            },
        },
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        down = [a for a in alerts if a["id"] == "call_site:3_micro_reflection"]
        assert len(down) == 1
        assert down[0]["severity"] == "WARNING"
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_call_site_alert_suppressed_for_disabled_status():
    """A call site in 'disabled' status (no API keys) MUST NOT emit an alert.

    Regression test for the call-site-11 spam loop: providers with no API key
    caused ghost-down status → CRITICAL alert → Sentinel wake every 5 min.
    Disabled is a config state, not an infrastructure alert.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {
            "11_user_model_synthesis": {
                "status": "disabled",
                "disabled_reason": "no_api_keys_configured",
            },
        },
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        call_site_alerts = [a for a in alerts if a["id"].startswith("call_site:")]
        assert call_site_alerts == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_call_site_alert_suppressed_for_unwired_groundwork():
    """A groundwork call site (meta.wired=False) MUST NOT emit an alert.

    Call sites with config but no runtime invocation are placeholders, not
    infrastructure. Alerting on them would be noise — the "call site" isn't
    actually part of any active code path.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {
            "10_cognitive_state": {  # meta.wired=False (planned for V4)
                "status": "down",
                "active_provider": None,
            },
        },
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert [a for a in alerts if a["id"].startswith("call_site:")] == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_fires_on_disk_low():
    """Disk <10% free should fire alert."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {"disk": {"free_pct": 7.2, "free_gb": 10.1}},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        disk_alerts = [a for a in alerts if a["id"] == "infra:disk_low"]
        assert len(disk_alerts) == 1
        assert disk_alerts[0]["severity"] == "CRITICAL"
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_fires_on_guardian_heartbeat_stale():
    """Guardian probe status=down must emit guardian:heartbeat_stale CRITICAL.

    Part 8: the Guardian is the host-side safety net. Its heartbeat going
    stale means the container has lost external visibility on itself,
    which is a Tier 1 defense mechanism failure.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {
            "guardian": {"status": "down", "staleness_s": 612.5},
        },
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        guardian_alerts = [a for a in alerts if a["id"] == "guardian:heartbeat_stale"]
        assert len(guardian_alerts) == 1
        assert guardian_alerts[0]["severity"] == "CRITICAL"
        # Message surfaces staleness for context
        assert "612" in guardian_alerts[0]["message"]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_skips_guardian_when_healthy():
    """Healthy Guardian must NOT emit any alert."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {
            "guardian": {"status": "healthy", "staleness_s": 5.2},
        },
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert [a for a in alerts if a["id"] == "guardian:heartbeat_stale"] == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_skips_guardian_when_paused():
    """Guardian paused → probe returns 'degraded', NOT an alert.

    Genesis paused by the user is a legitimate quiet state, not a failure.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {
            "guardian": {"status": "degraded", "message": "Guardian paused"},
        },
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert [a for a in alerts if a["id"] == "guardian:heartbeat_stale"] == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_handles_missing_staleness_gracefully():
    """If the Guardian probe details don't include staleness_s, still fire
    the alert with a best-effort message.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {
            "guardian": {"status": "down", "message": "signal_dropped"},
        },
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        guardian_alerts = [a for a in alerts if a["id"] == "guardian:heartbeat_stale"]
        assert len(guardian_alerts) == 1
        # Still emits; doesn't crash on missing staleness_s
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


# ── CC quota / rate-limit cross-check tests (Part 9b) ───────────────────


async def test_cc_quota_exhausted_suppressed_when_bg_healthy():
    """If the resilience state machine says RATE_LIMITED but the
    background budget tracker reports healthy, suppress the alert.
    The state machine is stale — the budget tracker is source of truth.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {
            "realtime_status": "RATE_LIMITED",
            "background": {"status": "healthy", "active": 0, "hourly_budget": "1/20"},
        },
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert [a for a in alerts if a["id"] == "cc:quota_exhausted"] == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_cc_quota_exhausted_fires_when_bg_also_throttled():
    """If BOTH the state machine and the budget tracker agree CC is
    degraded, the alert fires (at WARNING, not CRITICAL).
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {
            "realtime_status": "RATE_LIMITED",
            "background": {"status": "throttled", "active": 0, "hourly_budget": "20/20"},
        },
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        quota = [a for a in alerts if a["id"] == "cc:quota_exhausted"]
        assert len(quota) == 1
        assert quota[0]["severity"] == "WARNING"
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_cc_quota_exhausted_is_warning_not_critical():
    """Lock in the severity downgrade. Emission MUST be WARNING.
    Part 9c classifier routing assumes this — if this flips back to
    CRITICAL, the blanket rule will promote the alert to Tier 2 and
    the self-defeating Sentinel wake path is back.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {
            "realtime_status": "UNAVAILABLE",
            "background": {"status": "rate_limited", "active": 0, "hourly_budget": "20/20"},
        },
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        quota = [a for a in alerts if a["id"] == "cc:quota_exhausted"]
        assert len(quota) == 1
        assert quota[0]["severity"] == "WARNING"
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


# ── session_set_model / session_set_effort tests ─────────────────────────


async def test_session_set_model_invalid():
    result = await _impl_session_set_model("sess-1", "gpt4")
    assert "error" in result
    assert "Invalid model" in result["error"]


async def test_session_set_effort_invalid():
    result = await _impl_session_set_effort("sess-1", "turbo")
    assert "error" in result
    assert "Invalid effort" in result["error"]


async def test_session_set_model_no_service():
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = None
        result = await _impl_session_set_model("sess-1", "opus")
        assert "error" in result
        assert "not available" in result["error"]
    finally:
        health_mcp_mod._service = old


async def test_session_set_model_success():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=True) as mock_update:
            result = await _impl_session_set_model("sess-1", "opus")
            assert result["success"] is True
            assert result["model"] == "opus"
            mock_update.assert_awaited_once_with(
                mock_svc._db, "sess-1", model="opus",
            )
    finally:
        health_mcp_mod._service = old


async def test_session_set_model_not_found():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=False):
            result = await _impl_session_set_model("bad-id", "opus")
            assert "error" in result
            assert "not found" in result["error"]
    finally:
        health_mcp_mod._service = old


async def test_session_set_effort_success():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=True) as mock_update:
            result = await _impl_session_set_effort("sess-1", "high")
            assert result["success"] is True
            assert result["effort"] == "high"
            mock_update.assert_awaited_once_with(
                mock_svc._db, "sess-1", effort="high",
            )
    finally:
        health_mcp_mod._service = old


async def test_session_set_model_case_insensitive():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=True):
            result = await _impl_session_set_model("sess-1", "OPUS")
            assert result["success"] is True
            assert result["model"] == "opus"
    finally:
        health_mcp_mod._service = old


async def test_session_set_effort_no_service():
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = None
        result = await _impl_session_set_effort("sess-1", "high")
        assert "error" in result
        assert "not available" in result["error"]
    finally:
        health_mcp_mod._service = old


async def test_session_set_effort_not_found():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=False):
            result = await _impl_session_set_effort("bad-id", "high")
            assert "error" in result
            assert "not found" in result["error"]
    finally:
        health_mcp_mod._service = old


async def test_session_set_model_empty_session_id():
    result = await _impl_session_set_model("", "opus")
    assert "error" in result
    assert "required" in result["error"].lower()


async def test_session_set_effort_empty_session_id():
    result = await _impl_session_set_effort("  ", "high")
    assert "error" in result
    assert "required" in result["error"].lower()


class TestBackupAlertGate:
    """Backup alerts are gated on backups being ENABLED on this install
    (GENESIS_BACKUP_REPO). A failed/stale status file on an install where
    backups are intentionally disabled is a dishonest CRITICAL — it wedged
    the Sentinel for 26 days via a rejected approval that could never be
    re-resolved. Where backups ARE enabled, real failures still alert.
    """

    def _svc(self):
        svc = AsyncMock()
        svc.snapshot.return_value = {
            "call_sites": {},
            "cc_sessions": {"background": {}},
            "infrastructure": {},
            "queues": {},
            "awareness": {},
        }
        return svc

    def _fake_home(self, tmp_path, status: dict | None):
        import json as _json

        fake_home = tmp_path / "fakehome"
        (fake_home / ".genesis").mkdir(parents=True)
        if status is not None:
            (fake_home / ".genesis" / "backup_status.json").write_text(
                _json.dumps(status),
            )
        return fake_home

    async def _alerts(self, monkeypatch, tmp_path, *, status, enabled):
        from pathlib import Path

        fake_home = self._fake_home(tmp_path, status)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(
            "genesis.mcp.health.errors._backups_enabled", lambda: enabled,
        )
        old_service = health_mcp_mod._service
        old_history = health_mcp_mod._alert_history.copy()
        try:
            health_mcp_mod._service = self._svc()
            health_mcp_mod._alert_history = {}
            return await _impl_health_alerts()
        finally:
            health_mcp_mod._service = old_service
            health_mcp_mod._alert_history = old_history

    async def test_disabled_install_emits_no_backup_alerts(self, monkeypatch, tmp_path):
        alerts = await self._alerts(
            monkeypatch, tmp_path,
            status={"timestamp": "2026-07-02T18:38:34Z", "success": False,
                    "failure_reason": "Cannot clone backup repo without GENESIS_BACKUP_REPO"},
            enabled=False,
        )
        assert not [a for a in alerts if a["id"].startswith("backup:")]

    async def test_disabled_install_no_not_configured_nag(self, monkeypatch, tmp_path):
        alerts = await self._alerts(monkeypatch, tmp_path, status=None, enabled=False)
        assert not [a for a in alerts if a["id"].startswith("backup:")]

    async def test_enabled_install_failed_backup_still_alerts(self, monkeypatch, tmp_path):
        alerts = await self._alerts(
            monkeypatch, tmp_path,
            status={"timestamp": "2026-07-02T18:38:34Z", "success": False,
                    "failure_reason": "push failed"},
            enabled=True,
        )
        failed = [a for a in alerts if a["id"] == "backup:last_failed"]
        assert len(failed) == 1
        assert failed[0]["severity"] == "CRITICAL"

    async def test_enabled_install_missing_status_still_nags(self, monkeypatch, tmp_path):
        alerts = await self._alerts(monkeypatch, tmp_path, status=None, enabled=True)
        assert [a for a in alerts if a["id"] == "backup:not_configured"]


class TestBackupsEnabledHelper:
    """_backups_enabled: env first (server process), secrets.env file as
    fallback (the standalone MCP server only imports an allowlist of vars,
    so the env alone under-reports there)."""

    def test_env_var_set(self, monkeypatch):
        from genesis.mcp.health.errors import _backups_enabled

        monkeypatch.setenv("GENESIS_BACKUP_REPO", "https://example.com/o/backups.git")
        assert _backups_enabled() is True

    def test_unset_everywhere(self, monkeypatch, tmp_path):
        from pathlib import Path

        from genesis.mcp.health.errors import _backups_enabled

        monkeypatch.delenv("GENESIS_BACKUP_REPO", raising=False)
        # Signal #3 (an existing backup clone under the real home) must not
        # leak install state into the test — pin home to tmp_path.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "genesis.env.secrets_path", lambda: tmp_path / "secrets.env",
        )
        assert _backups_enabled() is False

    def test_secrets_file_fallback(self, monkeypatch, tmp_path):
        from pathlib import Path

        from genesis.mcp.health.errors import _backups_enabled

        monkeypatch.delenv("GENESIS_BACKUP_REPO", raising=False)
        # Signal #3 (an existing backup clone under the real home) must not
        # leak install state into the test — pin home to tmp_path.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        secrets = tmp_path / "secrets.env"
        secrets.write_text(
            "API_KEY_DEEPINFRA=abc\n"
            'GENESIS_BACKUP_REPO="https://example.com/o/backups.git"\n'
        )
        monkeypatch.setattr("genesis.env.secrets_path", lambda: secrets)
        assert _backups_enabled() is True

    def test_secrets_file_empty_value(self, monkeypatch, tmp_path):
        from pathlib import Path

        from genesis.mcp.health.errors import _backups_enabled

        monkeypatch.delenv("GENESIS_BACKUP_REPO", raising=False)
        # Signal #3 (an existing backup clone under the real home) must not
        # leak install state into the test — pin home to tmp_path.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        secrets = tmp_path / "secrets.env"
        secrets.write_text("GENESIS_BACKUP_REPO=\n")
        monkeypatch.setattr("genesis.env.secrets_path", lambda: secrets)
        assert _backups_enabled() is False

    def test_existing_clone_counts_as_enabled(self, monkeypatch, tmp_path):
        """backup.sh only needs GENESIS_BACKUP_REPO for the FIRST clone —
        an existing clone (e.g. created by restore.sh --from <url>) keeps
        backing up off the clone's remote with the var never persisted.
        Such an install must still alert on failures.
        """
        from pathlib import Path

        from genesis.mcp.health.errors import _backups_enabled

        fake_home = tmp_path / "fakehome"
        (fake_home / "backups" / "genesis-backups" / ".git").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.delenv("GENESIS_BACKUP_REPO", raising=False)
        assert _backups_enabled() is True


def _mock_snapshot():
    return {
        "call_sites": {}, "cc_sessions": {"background": {}},
        "infrastructure": {}, "queues": {}, "awareness": {},
    }


async def test_creds_corrupt_and_restored_alerts(monkeypatch, tmp_path):
    """A cred_integrity_status.json with corrupt + restored targets must emit
    creds:corrupt and creds:restored CRITICAL alerts."""
    import json
    from pathlib import Path

    fake_home = tmp_path / "home"
    (fake_home / ".genesis").mkdir(parents=True)
    (fake_home / ".genesis" / "cred_integrity_status.json").write_text(json.dumps({
        "version": 1, "checked_at": "2026-07-10T00:00:00+00:00",
        "targets": {
            "secrets_env": {"status": "corrupt", "detail": "nul_bytes"},
            "gh_hosts": {"status": "restored", "backup_mtime": "2026-07-09T18:00:00+00:00"},
            "claude_json": {"status": "ok"},
        },
    }))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = _mock_snapshot()
    old, old_history = health_mcp_mod._service, health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        corrupt = [a for a in alerts if a["id"] == "creds:corrupt"]
        restored = [a for a in alerts if a["id"] == "creds:restored"]
        assert len(corrupt) == 1 and corrupt[0]["severity"] == "CRITICAL"
        assert "secrets_env" in corrupt[0]["message"]
        assert len(restored) == 1 and restored[0]["severity"] == "CRITICAL"
        assert "gh_hosts" in restored[0]["message"]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_corrupt_pending_does_not_alert(monkeypatch, tmp_path):
    """corrupt_pending is the 2-tick debounce window — it must NOT fire an alert
    (else the debounce fails to suppress the false alarm it exists to prevent).
    Malformed-but-valid-JSON status must also not crash the alert pass."""
    import json
    from pathlib import Path

    fake_home = tmp_path / "home"
    (fake_home / ".genesis").mkdir(parents=True)
    (fake_home / ".genesis" / "cred_integrity_status.json").write_text(json.dumps({
        "version": 1,
        "targets": {
            "claude_json": {"status": "corrupt_pending", "detail": "parse_error"},
            "bogus": "not-a-dict",  # must be tolerated, not crash
        },
    }))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = _mock_snapshot()
    old, old_history = health_mcp_mod._service, health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert not [a for a in alerts if a["id"].startswith("creds:")]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_no_creds_alert_when_all_ok(monkeypatch, tmp_path):
    import json
    from pathlib import Path

    fake_home = tmp_path / "home"
    (fake_home / ".genesis").mkdir(parents=True)
    (fake_home / ".genesis" / "cred_integrity_status.json").write_text(json.dumps({
        "version": 1, "targets": {"secrets_env": {"status": "ok"}},
    }))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = _mock_snapshot()
    old, old_history = health_mcp_mod._service, health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert not [a for a in alerts if a["id"].startswith("creds:")]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history
