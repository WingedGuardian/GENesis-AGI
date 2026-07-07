"""Unit tests for the pure ambient-health evaluator + the bridge snapshot.

Covers the alert-decision logic (the testable core) and ``bridge_snapshot``'s
composition/contract; the SSH read + scheduler wiring are verified on-device
(they need the edge + a running scheduler).
"""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from genesis.observability import ambient_health
from genesis.observability.ambient_health import (
    AmbientRemoteConfig,
    AmbientRemoteConfigError,
    evaluate_ambient_health,
    load_ambient_remote_config,
)

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


def _snapshot(**overrides) -> dict:
    base = {
        "ts": NOW.isoformat(),
        "active_connections": 1,
        "diar_enabled": True,
        "diar_worker_alive": True,
    }
    base.update(overrides)
    return base


def test_healthy_snapshot_is_ok():
    assert evaluate_ambient_health(_snapshot(), now=NOW).status == "ok"


def test_none_data_is_unknown():
    # Transient SSH failure must not be reported as a hard "down".
    assert evaluate_ambient_health(None, now=NOW).status == "unknown"


def test_stale_heartbeat_is_down():
    stale = (NOW - timedelta(minutes=10)).isoformat()
    verdict = evaluate_ambient_health(_snapshot(ts=stale), now=NOW)
    assert verdict.status == "down"
    assert any("stale" in r for r in verdict.reasons)


def test_missing_ts_is_down():
    assert evaluate_ambient_health(_snapshot(ts=None), now=NOW).status == "down"


def test_device_offline_is_not_a_fault():
    # Device absent (active_connections=0) with a fresh heartbeat + live worker is
    # NOT a software bug — must NOT alert (policy: only software failures alert).
    assert evaluate_ambient_health(_snapshot(active_connections=0), now=NOW).status == "ok"


def test_device_offline_does_not_mask_software_failure():
    # A real software failure (dead diar worker) still fires even if the device
    # happens to be offline at the same time.
    verdict = evaluate_ambient_health(
        _snapshot(active_connections=0, diar_worker_alive=False), now=NOW,
    )
    assert verdict.status == "degraded"


def test_dead_diar_worker_is_degraded():
    verdict = evaluate_ambient_health(_snapshot(diar_worker_alive=False), now=NOW)
    assert verdict.status == "degraded"


def test_quiet_room_old_last_ts_is_still_ok():
    # No recent utterance (quiet room) must NOT be treated as a fault.
    old = (NOW - timedelta(hours=3)).isoformat()
    assert evaluate_ambient_health(_snapshot(last_ts=old), now=NOW).status == "ok"


def test_diar_disabled_does_not_degrade():
    # If diarization is off, a False worker flag is irrelevant.
    snap = _snapshot(diar_enabled=False, diar_worker_alive=False)
    assert evaluate_ambient_health(snap, now=NOW).status == "ok"


# --- load_ambient_remote_config: absent/disabled -> None; present-but-malformed -> raise ---


def _point_config_at(tmp_path, monkeypatch, text):
    cfg = tmp_path / "ambient_remote.yaml"
    cfg.write_text(text)
    monkeypatch.setattr(ambient_health, "_CONFIG_PATH", cfg)


def test_load_absent_returns_none(tmp_path, monkeypatch):
    # No config file -> legit no-op (install without an ambient edge).
    monkeypatch.setattr(ambient_health, "_CONFIG_PATH", tmp_path / "absent.yaml")
    assert load_ambient_remote_config() is None


def test_load_disabled_returns_none(tmp_path, monkeypatch):
    # enabled: false -> intentional disable, also a legit no-op (no raise).
    _point_config_at(tmp_path, monkeypatch, "enabled: false\nhost_ip: x\nhost_user: y\n")
    assert load_ambient_remote_config() is None


def test_load_valid_returns_config(tmp_path, monkeypatch):
    _point_config_at(tmp_path, monkeypatch, "host_ip: 192.0.2.9\nhost_user: edge\nenabled: true\n")
    cfg = load_ambient_remote_config()
    assert isinstance(cfg, AmbientRemoteConfig)
    assert cfg.host_ip == "192.0.2.9"
    assert cfg.host_user == "edge"


def test_load_missing_host_user_raises(tmp_path, monkeypatch):
    # Present + enabled but missing host_user -> misconfigured: MUST raise (visible),
    # not silently return None and look identical to "not configured".
    _point_config_at(tmp_path, monkeypatch, "host_ip: 192.0.2.9\nenabled: true\n")
    with pytest.raises(AmbientRemoteConfigError):
        load_ambient_remote_config()


def test_load_missing_host_ip_raises(tmp_path, monkeypatch):
    # The other branch of the `not host_ip or not host_user` guard.
    _point_config_at(tmp_path, monkeypatch, "host_user: edge\nenabled: true\n")
    with pytest.raises(AmbientRemoteConfigError):
        load_ambient_remote_config()


def test_load_unparseable_raises(tmp_path, monkeypatch):
    # Broken YAML (unterminated flow sequence) -> raise, not silent None.
    _point_config_at(tmp_path, monkeypatch, "host_ip: [1, 2\n")
    with pytest.raises(AmbientRemoteConfigError):
        load_ambient_remote_config()


# --- bridge_snapshot: the voice Bridge tab's one-call data contract ---
# Composes load_ambient_remote_config + read_edge_health + evaluate_ambient_health;
# must NEVER raise (the dashboard route serializes whatever comes back).


def _cfg() -> AmbientRemoteConfig:
    return AmbientRemoteConfig(host_ip="192.0.2.9", host_user="edge")


def test_bridge_snapshot_not_configured(monkeypatch):
    monkeypatch.setattr(ambient_health, "load_ambient_remote_config", lambda: None)
    out = asyncio.run(ambient_health.bridge_snapshot())
    assert out == {"configured": False, "reason": "no ambient edge configured"}


def test_bridge_snapshot_misconfigured(monkeypatch):
    # Present-but-broken config must surface VISIBLY (not raise, not look absent).
    def _raise():
        raise AmbientRemoteConfigError("enabled but missing host_ip/host_user")

    monkeypatch.setattr(ambient_health, "load_ambient_remote_config", _raise)
    out = asyncio.run(ambient_health.bridge_snapshot())
    assert out["configured"] is True
    assert out["reachable"] is False
    assert out["verdict"] == "misconfigured"
    assert any("host_ip" in r for r in out["reasons"])
    assert "health" not in out


def test_bridge_snapshot_unreachable_edge(monkeypatch):
    monkeypatch.setattr(ambient_health, "load_ambient_remote_config", _cfg)

    async def _none(cfg):
        return None

    monkeypatch.setattr(ambient_health, "read_edge_health", _none)
    out = asyncio.run(ambient_health.bridge_snapshot(now=NOW))
    assert out["configured"] is True
    assert out["reachable"] is False
    assert out["verdict"] == "unknown"
    assert isinstance(out["latency_ms"], float)
    assert "health" not in out


def test_bridge_snapshot_happy_passthrough(monkeypatch):
    monkeypatch.setattr(ambient_health, "load_ambient_remote_config", _cfg)
    snap = _snapshot(rss_total_mb=438.0, ort_arena_off=True)

    async def _data(cfg):
        return snap

    monkeypatch.setattr(ambient_health, "read_edge_health", _data)
    out = asyncio.run(ambient_health.bridge_snapshot(now=NOW))
    assert out["configured"] is True
    assert out["reachable"] is True
    assert out["verdict"] == "ok"
    assert out["reasons"] == ["healthy"]
    # Full passthrough under a "health" sub-key — no filtering, so future edge
    # keys surface without a Genesis-side change.
    assert out["health"]["rss_total_mb"] == 438.0
    assert isinstance(out["latency_ms"], float)


def test_bridge_snapshot_verdict_logic_reused_not_reimplemented(monkeypatch):
    # A stale heartbeat must flow through evaluate_ambient_health as "down":
    # the SSH read WORKED (reachable=True) — it's the bridge process that's dead.
    monkeypatch.setattr(ambient_health, "load_ambient_remote_config", _cfg)
    stale = _snapshot(ts=(NOW - timedelta(minutes=10)).isoformat())

    async def _data(cfg):
        return stale

    monkeypatch.setattr(ambient_health, "read_edge_health", _data)
    out = asyncio.run(ambient_health.bridge_snapshot(now=NOW))
    assert out["reachable"] is True
    assert out["verdict"] == "down"
    assert any("stale" in r for r in out["reasons"])


# --- RSS-threshold alert: leak-class regression watch on the health snapshot ---
# Thresholds sit ~2x the worst observed post-arena-off burst (soak closed
# 2026-07-06: total plateau ~470 MB / bursts ~780; child flat ~170 / bursts ~280)
# so workload breathing never fires them, only a real regression does.


def test_rss_total_over_ceiling_is_degraded():
    verdict = evaluate_ambient_health(_snapshot(rss_total_mb=1200.0), now=NOW)
    assert verdict.status == "degraded"
    assert any("RSS" in r and "1200" in r for r in verdict.reasons)


def test_rss_child_over_ceiling_is_degraded():
    verdict = evaluate_ambient_health(_snapshot(rss_diar_child_mb=600.0), now=NOW)
    assert verdict.status == "degraded"
    assert any("diar child" in r for r in verdict.reasons)


def test_rss_at_plateau_is_ok():
    verdict = evaluate_ambient_health(
        _snapshot(rss_total_mb=470.0, rss_diar_child_mb=170.0, rss_parent_mb=300.0),
        now=NOW,
    )
    assert verdict.status == "ok"


def test_rss_keys_absent_or_null_do_not_trigger():
    # Lazy pool spawn → child key is null until the first diar window; absent
    # keys (older edge) must also be a no-op. Null/absent != breach.
    assert evaluate_ambient_health(_snapshot(rss_diar_child_mb=None), now=NOW).status == "ok"
    assert evaluate_ambient_health(_snapshot(), now=NOW).status == "ok"


def test_rss_non_numeric_is_ignored():
    verdict = evaluate_ambient_health(_snapshot(rss_total_mb="lots"), now=NOW)
    assert verdict.status == "ok"


def test_rss_breach_does_not_mask_down():
    # A dead bridge (stale heartbeat) stays "down" even when RSS also breaches;
    # the RSS reason is still appended for the operator.
    stale = (NOW - timedelta(minutes=10)).isoformat()
    verdict = evaluate_ambient_health(
        _snapshot(ts=stale, rss_total_mb=1200.0), now=NOW,
    )
    assert verdict.status == "down"
    assert any("RSS" in r for r in verdict.reasons)


def test_causes_are_stable_machine_keys():
    # Consumers (the outreach alert state machine) need value-free keys to
    # detect a NEW fault while already degraded — reason strings embed live
    # numbers, and the bare status can't distinguish causes.
    assert evaluate_ambient_health(_snapshot(), now=NOW).causes == ()
    assert evaluate_ambient_health(None, now=NOW).causes == ("unreachable",)
    stale = (NOW - timedelta(minutes=10)).isoformat()
    assert evaluate_ambient_health(_snapshot(ts=stale), now=NOW).causes == ("bridge-dead",)
    assert evaluate_ambient_health(
        _snapshot(diar_worker_alive=False), now=NOW,
    ).causes == ("diar-worker",)
    verdict = evaluate_ambient_health(
        _snapshot(rss_total_mb=1200.0, rss_diar_child_mb=600.0), now=NOW,
    )
    assert verdict.causes == ("rss-total", "rss-diar-child")


def test_bridge_snapshot_carries_causes(monkeypatch):
    monkeypatch.setattr(ambient_health, "load_ambient_remote_config", _cfg)
    snap = _snapshot(rss_total_mb=1200.0)

    async def _data(cfg):
        return snap

    monkeypatch.setattr(ambient_health, "read_edge_health", _data)
    out = asyncio.run(ambient_health.bridge_snapshot(now=NOW))
    assert out["causes"] == ["rss-total"]
