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


async def test_step_in_success_with_unreachable_recheck_is_not_failed(cfg, monkeypatch):
    """Restore reports ok but the post-restore recheck is unreachable (None) →
    must NOT be reported as FAILED (recheck-unreachable = inconclusive)."""
    old = (datetime.now(UTC) - timedelta(minutes=40)).isoformat()
    (cfg.state_path).mkdir(parents=True, exist_ok=True)
    _seed_state(cfg, {"secrets_env": {
        "first_seen": old, "warned_at": old, "stepped_in_at": None,
        "last_alert_at": old, "restore_attempts": [],
    }})
    checks = iter([_report(secrets_env="nul_bytes"), None])  # 2nd check unreachable
    monkeypatch.setattr(cred_watch, "run_container_check", lambda config: _next(checks))

    async def fake_restore(config, name):
        return {"ok": True, "action": "restored", "backup_mtime": "2026-07-09T18:00:00+00:00"}

    monkeypatch.setattr(cred_watch, "run_container_restore", fake_restore)
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert any(a.severity == AlertSeverity.CRITICAL and "restored" in a.title.lower()
               for a in disp.alerts)
    assert not any("FAILED" in a.title for a in disp.alerts)


async def test_unreadable_past_grace_alerts_but_never_restores(cfg, monkeypatch):
    """An 'unreadable' verdict is ambiguous (not RESTORABLE) — past grace the
    guardian must ALERT but NOT call run_container_restore."""
    old = (datetime.now(UTC) - timedelta(minutes=40)).isoformat()
    (cfg.state_path).mkdir(parents=True, exist_ok=True)
    _seed_state(cfg, {"secrets_env": {
        "first_seen": old, "warned_at": old, "stepped_in_at": None,
        "last_alert_at": old, "restore_attempts": [],
    }})
    monkeypatch.setattr(cred_watch, "run_container_check",
                        _async(_report(secrets_env="unreadable")))
    called = {"restore": 0}

    async def fake_restore(config, name):
        called["restore"] += 1
        return {"ok": True, "action": "restored"}

    monkeypatch.setattr(cred_watch, "run_container_restore", fake_restore)
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert called["restore"] == 0
    assert any(a.severity == AlertSeverity.CRITICAL and "unreadable" in a.title.lower()
               for a in disp.alerts)


async def test_run_container_restore_rejects_unknown_target(cfg):
    result = await cred_watch.run_container_restore(cfg, "totally_bogus_target")
    assert result is None


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


# ── G.4 mirror freshness + host-only archive hop ────────────────────────────

import os  # noqa: E402


def _mirror(cfg, *, stamp=True, rels=("creds/x.gpg", "secrets/secrets.env.gpg"),
            age_h=0.0):
    """Build a host-view mirror under <state>/shared/guardian/creds-mirror."""
    mdir = cfg.state_path / "shared" / "guardian" / "creds-mirror"
    mdir.mkdir(parents=True, exist_ok=True)
    for rel in rels:
        p = mdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ENC")
        if age_h:
            t = (datetime.now(UTC) - timedelta(hours=age_h)).timestamp()
            os.utime(p, (t, t))
    if stamp:
        (mdir / "MIRROR_STAMP").write_text("mirrored_at=x\ncount=2\n")
    return mdir


def _escrow(cfg):
    esc = cfg.state_path / "shared" / "guardian" / "backup_passphrase.env"
    esc.parent.mkdir(parents=True, exist_ok=True)
    esc.write_text("GENESIS_BACKUP_PASSPHRASE=pw\n")
    return esc


async def test_mirror_fresh_no_alert(cfg):
    _escrow(cfg)
    _mirror(cfg, age_h=1.0)
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    assert disp.alerts == []


async def test_mirror_stale_warns(cfg):
    _escrow(cfg)
    _mirror(cfg, age_h=60.0)  # > 48h default
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    assert len(disp.alerts) == 1
    assert disp.alerts[0].severity == AlertSeverity.WARNING
    assert "stale" in disp.alerts[0].title.lower()
    assert (cfg.state_path / "mirror_alert_state.json").exists()


async def test_mirror_missing_stamp_warns(cfg):
    _escrow(cfg)
    _mirror(cfg, stamp=False, age_h=1.0)  # fresh files but incomplete round
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    assert len(disp.alerts) == 1
    assert disp.alerts[0].severity == AlertSeverity.WARNING


async def test_mirror_no_escrow_is_silent(cfg):
    # No escrow ⇒ backups not configured ⇒ no freshness judgement.
    _mirror(cfg, stamp=False, age_h=99.0)
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    assert disp.alerts == []


async def test_mirror_stale_realert_damped(cfg):
    _escrow(cfg)
    _mirror(cfg, age_h=60.0)
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    await cred_watch._check_mirror_and_archive(cfg, disp)  # within realert window
    assert len(disp.alerts) == 1  # second call damped


async def test_mirror_recovered_sends_info(cfg):
    _escrow(cfg)
    _mirror(cfg, age_h=60.0)
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)  # WARNING + state
    # Freshen the mirror, re-run → INFO recovery + state cleared.
    _mirror(cfg, age_h=0.0)
    await cred_watch._check_mirror_and_archive(cfg, disp)
    assert disp.alerts[-1].severity == AlertSeverity.INFO
    state = (cfg.state_path / "mirror_alert_state.json").read_text()
    assert "warned_at" not in state


async def test_archive_populates_from_mirror(cfg):
    _escrow(cfg)
    _mirror(cfg, age_h=1.0)
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    arc = cfg.state_path / "creds-archive"
    assert (arc / "creds" / "x.gpg").exists()
    assert (arc / "secrets" / "secrets.env.gpg").exists()
    assert (arc / "MIRROR_STAMP").exists()
    assert (arc / "backup_passphrase.env").read_text() == "GENESIS_BACKUP_PASSPHRASE=pw\n"


async def test_archive_refuses_empty_mirror(cfg):
    # Seed a prior archive, then present a stamp-present but gpg-empty mirror.
    arc = cfg.state_path / "creds-archive"
    (arc / "creds").mkdir(parents=True)
    (arc / "creds" / "old.gpg").write_bytes(b"OLD")
    (arc / "MIRROR_STAMP").write_text("old\n")
    mdir = cfg.state_path / "shared" / "guardian" / "creds-mirror"
    mdir.mkdir(parents=True)
    (mdir / "MIRROR_STAMP").write_text("x\n")  # stamp present, NO gpg files
    cred_watch._archive_mirror(cfg, mdir, cfg.state_path / "nope-escrow")
    assert (arc / "creds" / "old.gpg").read_bytes() == b"OLD"  # untouched


async def test_archive_refuses_stampless_mirror(cfg):
    arc = cfg.state_path / "creds-archive"
    (arc).mkdir(parents=True)
    (arc / "sentinel").write_text("keep")
    mdir = _mirror(cfg, stamp=False)  # gpg present, no STAMP
    cred_watch._archive_mirror(cfg, mdir, cfg.state_path / "nope")
    assert (arc / "sentinel").read_text() == "keep"
    assert not (arc / "creds" / "x.gpg").exists()


async def test_archive_is_grow_only_partial_mirror_keeps_creds(cfg):
    """The last-line archive must NEVER lose a cred to a transient/partial mirror
    (e.g. a backup.sh rewrite window) — it is grow-only."""
    _escrow(cfg)
    mdir = _mirror(cfg, rels=("creds/a.gpg", "creds/b.gpg"), age_h=1.0)
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    arc = cfg.state_path / "creds-archive"
    assert (arc / "creds" / "a.gpg").exists()
    assert (arc / "creds" / "b.gpg").exists()
    # b momentarily vanishes from the mirror (partial round) → archive KEEPS it.
    (mdir / "creds" / "b.gpg").unlink()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    assert (arc / "creds" / "b.gpg").exists()  # grow-only: last copy preserved
    assert (arc / "creds" / "a.gpg").exists()


async def test_archive_keeps_escrow_across_transient_read_miss(cfg):
    """A one-tick escrow read-miss must not drop the archived passphrase."""
    esc = _escrow(cfg)
    _mirror(cfg, age_h=1.0)
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)
    arc = cfg.state_path / "creds-archive"
    assert (arc / "backup_passphrase.env").exists()
    esc.unlink()  # transient miss
    await cred_watch._check_mirror_and_archive(cfg, disp)
    assert (arc / "backup_passphrase.env").exists()  # grow-only: kept


async def test_stale_warn_state_cleared_when_escrow_disappears(cfg):
    _escrow(cfg)
    _mirror(cfg, age_h=60.0)
    disp = _Dispatcher()
    await cred_watch._check_mirror_and_archive(cfg, disp)  # WARNING + state written
    assert "warned_at" in (cfg.state_path / "mirror_alert_state.json").read_text()
    (cfg.state_path / "shared" / "guardian" / "backup_passphrase.env").unlink()
    await cred_watch._check_mirror_and_archive(cfg, disp)  # escrow gone → cleared
    assert (cfg.state_path / "mirror_alert_state.json").read_text() == "{}"


async def test_orchestrator_runs_mirror_even_when_container_unreachable(cfg, monkeypatch):
    # container check returns None (no signal), but the host-side mirror/archive
    # still runs — the archive must be populated.
    _escrow(cfg)
    _mirror(cfg, age_h=1.0)
    monkeypatch.setattr(cred_watch, "run_container_check", _async(None))
    disp = _Dispatcher()
    await check_credential_integrity_and_alert(cfg, disp)
    assert (cfg.state_path / "creds-archive" / "creds" / "x.gpg").exists()
