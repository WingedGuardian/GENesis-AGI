"""Tests for the guardian-side credential-integrity watch (escalation ladder)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.guardian import cred_watch
from genesis.guardian.alert.base import AlertSeverity
from genesis.guardian.config import CredIntegrityConfig, GuardianConfig
from genesis.guardian.cred_watch import (
    check_credential_integrity_and_alert,
    decide,
)

CFG = CredIntegrityConfig(grace_minutes=30, realert_hours=6.0)
NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _episode(**kw):
    base = {"first_seen": NOW.isoformat(), "warned_at": NOW.isoformat(),
            "stepped_in_at": None, "last_alert_at": NOW.isoformat(),
            "restore_attempts": []}
    base.update(kw)
    return base


# ── decide() matrix ─────────────────────────────────────────────────────────


def test_decide_healthy_no_episode():
    assert decide("x", False, None, NOW, CFG).action == "none"


def test_decide_first_detection_warns():
    assert decide("x", True, None, NOW, CFG).action == "warn"


def test_decide_in_grace_defers():
    ep = _episode(first_seen=(NOW - timedelta(minutes=10)).isoformat())
    assert decide("x", True, ep, NOW, CFG).action == "none"


def test_decide_past_grace_steps_in():
    ep = _episode(first_seen=(NOW - timedelta(minutes=31)).isoformat())
    assert decide("x", True, ep, NOW, CFG).action == "step_in"


def test_decide_resolved_after_warn():
    ep = _episode()
    assert decide("x", False, ep, NOW, CFG).action == "resolved"


def test_decide_realert_after_stepin_on_cadence():
    ep = _episode(
        first_seen=(NOW - timedelta(hours=2)).isoformat(),
        stepped_in_at=(NOW - timedelta(hours=1)).isoformat(),
        last_alert_at=(NOW - timedelta(hours=7)).isoformat(),  # older than realert_hours
    )
    assert decide("x", True, ep, NOW, CFG).action == "realert"


def test_decide_stepped_in_within_realert_window_is_quiet():
    ep = _episode(
        stepped_in_at=(NOW - timedelta(hours=1)).isoformat(),
        last_alert_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    assert decide("x", True, ep, NOW, CFG).action == "none"


# ── _extract_json tolerance ─────────────────────────────────────────────────


def test_extract_json_skips_preamble():
    out = "Last login: whatever\n{\"version\": 1, \"results\": {}}\n"
    assert cred_watch._extract_json(out) == {"version": 1, "results": {}}


def test_extract_json_none_on_garbage():
    assert cred_watch._extract_json("no json here\n") is None


# ── Orchestrator ────────────────────────────────────────────────────────────


class _Dispatcher:
    def __init__(self):
        self.alerts = []

    async def send(self, alert):
        self.alerts.append(alert)
        return True


def _report(**statuses):
    return {"version": 1, "results": {
        n: {"ok": s == "ok", "status": s, "detail": s} for n, s in statuses.items()
    }}


@pytest.fixture
def cfg(tmp_path):
    return GuardianConfig(state_dir=str(tmp_path))


async def test_first_corruption_warns_and_records(cfg, monkeypatch):
    monkeypatch.setattr(cred_watch, "run_container_check",
                        _async(_report(secrets_env="nul_bytes")))
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert len(disp.alerts) == 1
    assert disp.alerts[0].severity == AlertSeverity.WARNING
    # State recorded.
    state = (cfg.state_path / "cred_alert_state.json").read_text()
    assert "secrets_env" in state and "first_seen" in state


async def test_step_in_restores_and_criticals(cfg, monkeypatch):
    # Pre-seed an episode past the grace window.
    old = (datetime.now(UTC) - timedelta(minutes=40)).isoformat()
    (cfg.state_path).mkdir(parents=True, exist_ok=True)
    _seed_state(cfg, {"secrets_env": {
        "first_seen": old, "warned_at": old, "stepped_in_at": None,
        "last_alert_at": old, "restore_attempts": [],
    }})
    checks = iter([_report(secrets_env="nul_bytes"), _report(secrets_env="ok")])
    monkeypatch.setattr(cred_watch, "run_container_check",
                        lambda config: _next(checks))
    restore_calls = {"n": 0}

    async def fake_restore(config, name):
        restore_calls["n"] += 1
        return {"ok": True, "action": "restored", "backup_mtime": "2026-07-09T18:00:00+00:00"}

    monkeypatch.setattr(cred_watch, "run_container_restore", fake_restore)
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert restore_calls["n"] == 1
    assert any(a.severity == AlertSeverity.CRITICAL and "restored" in a.title.lower()
               for a in disp.alerts)


async def test_exec_failure_no_alert_no_state_change(cfg, monkeypatch):
    monkeypatch.setattr(cred_watch, "run_container_check", _async(None))
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert disp.alerts == []
    assert not (cfg.state_path / "cred_alert_state.json").exists()


async def test_resolution_sends_info_and_clears(cfg, monkeypatch):
    (cfg.state_path).mkdir(parents=True, exist_ok=True)
    _seed_state(cfg, {"gh_hosts": _episode()})
    monkeypatch.setattr(cred_watch, "run_container_check",
                        _async(_report(gh_hosts="ok")))
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert any(a.severity == AlertSeverity.INFO for a in disp.alerts)
    assert '"gh_hosts"' not in (cfg.state_path / "cred_alert_state.json").read_text()


async def test_disabled_is_noop(cfg, monkeypatch):
    cfg.cred_integrity.enabled = False

    async def _should_not_run(config):
        raise AssertionError("check must not run when disabled")

    monkeypatch.setattr(cred_watch, "run_container_check", _should_not_run)
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert disp.alerts == []


async def test_absent_targets_do_not_alert(cfg, monkeypatch):
    monkeypatch.setattr(cred_watch, "run_container_check",
                        _async(_report(genesis_yaml="absent", secrets_env="ok")))
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert disp.alerts == []


# ── helpers ─────────────────────────────────────────────────────────────────


def _async(value):
    async def _fn(config):
        return value
    return _fn


def _next(it):
    async def _coro():
        return next(it)
    return _coro()


def _seed_state(cfg, episodes: dict) -> None:
    import json
    (cfg.state_path / "cred_alert_state.json").write_text(
        json.dumps({"version": 1, "episodes": episodes})
    )
