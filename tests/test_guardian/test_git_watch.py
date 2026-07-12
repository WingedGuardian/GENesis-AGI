"""Tests for the guardian-side git-health watch (git_watch, F.1).

The guardian live-probes the container's git via incus exec (the PRIMARY
detector) and escalates per a confirm/warn/realert/resolve ladder. Probe-exec
failure is NO signal. Covers the pure decision + parse functions and the
orchestrator (mocked probe + dispatcher).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.guardian import git_watch
from genesis.guardian.alert.base import AlertSeverity
from genesis.guardian.config import GitHealthConfig, GuardianConfig


class _Cfg:
    """Minimal config stub exposing what git_watch reads."""

    def __init__(self, tmp_path):
        self.container_name = "genesis"
        self.git_health = GitHealthConfig()
        self._sp = tmp_path

    @property
    def state_path(self):
        return self._sp


def _now():
    return datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


class TestParseProbe:
    def test_ok(self):
        assert git_watch._parse_probe("preamble\nGITHEALTH ok\n") == {
            "healthy": True,
            "failures": [],
        }

    def test_failures(self):
        r = git_watch._parse_probe("GITHEALTH head_unresolvable rootfs_readonly")
        assert r == {"healthy": False, "failures": ["head_unresolvable", "rootfs_readonly"]}

    def test_repo_missing(self):
        assert git_watch._parse_probe("GITHEALTH repo_missing") == {
            "healthy": False,
            "failures": ["repo_missing"],
        }

    def test_no_marker_returns_none(self):
        assert git_watch._parse_probe("garbage output, no marker") is None


class TestDecide:
    cfg = GitHealthConfig(confirm_ticks=2, realert_hours=6.0)

    def test_healthy_no_episode_none(self):
        d = git_watch.decide(False, None, _now(), self.cfg)
        assert d.action == "none"

    def test_healthy_after_warn_resolves(self):
        d = git_watch.decide(False, {"warned_at": _now().isoformat()}, _now(), self.cfg)
        assert d.action == "resolved"

    def test_first_unhealthy_confirming_not_warn(self):
        # consecutive=1 < confirm_ticks=2 → still confirming, no alert yet
        d = git_watch.decide(True, {"consecutive": 1}, _now(), self.cfg)
        assert d.action == "none"

    def test_confirmed_unhealthy_warns(self):
        d = git_watch.decide(True, {"consecutive": 2}, _now(), self.cfg)
        assert d.action == "warn"

    def test_realert_after_window(self):
        old = (_now() - timedelta(hours=7)).isoformat()
        ep = {"consecutive": 5, "warned_at": old, "last_alert_at": old}
        d = git_watch.decide(True, ep, _now(), self.cfg)
        assert d.action == "realert"

    def test_realert_damped_within_window(self):
        recent = (_now() - timedelta(hours=1)).isoformat()
        ep = {"consecutive": 5, "warned_at": recent, "last_alert_at": recent}
        d = git_watch.decide(True, ep, _now(), self.cfg)
        assert d.action == "none"


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_probe_none_is_no_signal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(git_watch, "probe_container_git", AsyncMock(return_value=None))
        disp = AsyncMock()
        await git_watch.check_container_git_and_alert(_Cfg(tmp_path), disp)
        disp.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmed_unhealthy_warns_and_persists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            git_watch,
            "probe_container_git",
            AsyncMock(return_value={"healthy": False, "failures": ["rootfs_readonly"]}),
        )
        disp = AsyncMock()
        cfg = _Cfg(tmp_path)
        # tick 1: confirming (consecutive 1 < 2) → no alert
        await git_watch.check_container_git_and_alert(cfg, disp)
        disp.send.assert_not_called()
        # tick 2: confirmed → WARN
        await git_watch.check_container_git_and_alert(cfg, disp)
        disp.send.assert_called_once()
        alert = disp.send.call_args.args[0]
        assert alert.severity == AlertSeverity.WARNING
        assert "recovery-and-portability" in alert.body
        assert (tmp_path / "git_alert_state.json").exists()

    @pytest.mark.asyncio
    async def test_recovery_sends_info_and_clears(self, tmp_path, monkeypatch):
        cfg = _Cfg(tmp_path)
        # seed a warned episode
        (tmp_path / "git_alert_state.json").write_text(
            '{"version": 1, "episode": {"consecutive": 3, "warned_at": "2026-07-11T00:00:00+00:00"}}'
        )
        monkeypatch.setattr(
            git_watch,
            "probe_container_git",
            AsyncMock(return_value={"healthy": True, "failures": []}),
        )
        disp = AsyncMock()
        await git_watch.check_container_git_and_alert(cfg, disp)
        disp.send.assert_called_once()
        assert disp.send.call_args.args[0].severity == AlertSeverity.INFO
        assert not (tmp_path / "git_alert_state.json").exists()

    @pytest.mark.asyncio
    async def test_disabled_no_probe(self, tmp_path, monkeypatch):
        cfg = _Cfg(tmp_path)
        cfg.git_health = GitHealthConfig(enabled=False)
        probe = AsyncMock(return_value={"healthy": False, "failures": ["x"]})
        monkeypatch.setattr(git_watch, "probe_container_git", probe)
        disp = AsyncMock()
        await git_watch.check_container_git_and_alert(cfg, disp)
        probe.assert_not_called()
        disp.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_orchestrator_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            git_watch, "probe_container_git", AsyncMock(side_effect=RuntimeError("boom"))
        )
        # Must be swallowed — a crashing probe must never break the guardian tick.
        await git_watch.check_container_git_and_alert(_Cfg(tmp_path), AsyncMock())


class TestContainerGitSupportsRevert:
    """The fail-open helper the guardian uses to redirect a doomed REVERT_CODE
    PROPOSAL to SNAPSHOT_ROLLBACK before approval (never a post-approval swap)."""

    @pytest.mark.asyncio
    async def test_healthy_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            git_watch,
            "probe_container_git",
            AsyncMock(return_value={"healthy": True, "failures": []}),
        )
        assert await git_watch.container_git_supports_revert(_Cfg(tmp_path)) is True

    @pytest.mark.asyncio
    async def test_unhealthy_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            git_watch,
            "probe_container_git",
            AsyncMock(return_value={"healthy": False, "failures": ["rootfs_readonly"]}),
        )
        assert await git_watch.container_git_supports_revert(_Cfg(tmp_path)) is False

    @pytest.mark.asyncio
    async def test_inconclusive_probe_fails_open(self, tmp_path, monkeypatch):
        # None (unreachable/unparseable) → fail OPEN so a flaky probe never blocks recovery.
        monkeypatch.setattr(git_watch, "probe_container_git", AsyncMock(return_value=None))
        assert await git_watch.container_git_supports_revert(_Cfg(tmp_path)) is True

    @pytest.mark.asyncio
    async def test_probe_raises_fails_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            git_watch, "probe_container_git", AsyncMock(side_effect=RuntimeError("boom"))
        )
        assert await git_watch.container_git_supports_revert(_Cfg(tmp_path)) is True


def test_config_defaults_present():
    # The guardian config must carry a git_health sub-config by default.
    c = GuardianConfig()
    assert isinstance(c.git_health, GitHealthConfig)
    assert c.git_health.enabled is True
