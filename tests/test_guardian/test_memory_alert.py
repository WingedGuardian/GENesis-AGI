"""Tests for the guardian-side RAM tiered alert (E-rest PR-E1, memory_watch.py).

Covers the pure two-axis worst-of tier logic, the host-VM measurement, and the
hysteresis alert flow (tier rise WARN→HIGH→CRIT, recovery INFO, sustained
realert, disabled silent, both-axes-blind no-op, host-only axis) using a real
GuardianConfig + a capturing dispatcher. Mirrors test_bundle_watch.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from genesis.guardian import memory_watch
from genesis.guardian.alert.base import AlertSeverity
from genesis.guardian.config import GuardianConfig, MemoryTiersConfig
from genesis.guardian.memory_watch import (
    check_memory_and_alert,
    measure_host_mem_pct,
    memory_status_snapshot,
    memory_worst_tier,
)


class _FakeDispatcher:
    def __init__(self):
        self.sent = []

    async def send(self, alert):
        self.sent.append(alert)
        return True


def _config(tmp_path: Path, **cfg_kw) -> GuardianConfig:
    config = GuardianConfig(state_dir=str(tmp_path))
    config.memory_tiers = MemoryTiersConfig(**cfg_kw)
    return config


def _patch_axes(container_pct, host_pct):
    """Patch both RAM measurement sources on the memory_watch module."""

    async def _container(_config):
        return container_pct, f"container {container_pct}"

    def _host():
        return host_pct, f"host {host_pct}"

    return (
        patch.object(memory_watch, "measure_container_mem_pct", _container),
        patch.object(memory_watch, "measure_host_mem_pct", _host),
    )


# ── pure tier logic ─────────────────────────────────────────────────────────


def test_worst_tier_matrix():
    cfg = MemoryTiersConfig()
    assert memory_worst_tier(None, None, cfg) == "ok"
    assert memory_worst_tier(10.0, 10.0, cfg) == "ok"
    assert memory_worst_tier(78.0, 10.0, cfg) == "warn"  # container 78 ≥ 75
    assert memory_worst_tier(88.0, 10.0, cfg) == "high"  # container 88 ≥ 85
    assert memory_worst_tier(95.0, 10.0, cfg) == "crit"  # container 95 ≥ 92
    # host axis has higher thresholds (80/88/94)
    assert memory_worst_tier(10.0, 82.0, cfg) == "warn"
    assert memory_worst_tier(10.0, 90.0, cfg) == "high"
    assert memory_worst_tier(10.0, 96.0, cfg) == "crit"
    # worst-of: the higher tier wins across axes
    assert memory_worst_tier(88.0, 82.0, cfg) == "high"  # container high beats host warn
    # a blind axis (None) never contributes
    assert memory_worst_tier(None, 82.0, cfg) == "warn"
    assert memory_worst_tier(95.0, None, cfg) == "crit"


def test_measure_host_mem_pct():
    # 20% available → 80% used
    with patch.object(
        memory_watch,
        "_read_meminfo",
        return_value={"MemTotal": 20 * 1024**2, "MemAvailable": 4 * 1024**2},
    ):
        pct, detail = measure_host_mem_pct()
    assert abs(pct - 80.0) < 0.01
    assert "80.0%" in detail


def test_measure_host_mem_pct_unreadable():
    with patch.object(memory_watch, "_read_meminfo", return_value={}):
        pct, detail = measure_host_mem_pct()
    assert pct is None
    assert "unreadable" in detail


# ── hysteresis alert flow ───────────────────────────────────────────────────


async def test_tier_rise_alerts_each_increase(tmp_path):
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    p_c, p_h = _patch_axes(78.0, 10.0)  # warn
    with p_c, p_h:
        await check_memory_and_alert(config, disp)
    assert len(disp.sent) == 1
    assert disp.sent[0].severity == AlertSeverity.WARNING
    assert "RAM WARN" in disp.sent[0].title

    p_c, p_h = _patch_axes(95.0, 10.0)  # crit — an increase, alerts immediately
    with p_c, p_h:
        await check_memory_and_alert(config, disp)
    assert len(disp.sent) == 2
    assert disp.sent[1].severity == AlertSeverity.CRITICAL
    assert "RAM CRIT" in disp.sent[1].title
    assert "container 95%" in disp.sent[1].body


async def test_recovery_emits_info(tmp_path):
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    p_c, p_h = _patch_axes(95.0, 10.0)  # crit
    with p_c, p_h:
        await check_memory_and_alert(config, disp)
    p_c, p_h = _patch_axes(10.0, 10.0)  # back to ok
    with p_c, p_h:
        await check_memory_and_alert(config, disp)
    assert len(disp.sent) == 2
    assert disp.sent[1].severity == AlertSeverity.INFO
    assert "recovered" in disp.sent[1].title.lower()


async def test_sustained_tier_damps_then_realerts(tmp_path):
    config = _config(tmp_path, realert_hours=6.0)
    disp = _FakeDispatcher()
    p_c, p_h = _patch_axes(88.0, 10.0)  # high
    with p_c, p_h:
        await check_memory_and_alert(config, disp)  # alert 1
        await check_memory_and_alert(config, disp)  # same tier, within window → damped
    assert len(disp.sent) == 1

    # backdate the persisted alert timestamp past the realert window
    state_file = config.state_path / "memory_alert_state.json"
    data = json.loads(state_file.read_text())
    data["last_alert_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    state_file.write_text(json.dumps(data))

    with p_c, p_h:
        await check_memory_and_alert(config, disp)  # sustained past window → realert
    assert len(disp.sent) == 2
    assert disp.sent[1].severity == AlertSeverity.WARNING  # HIGH → WARNING severity


async def test_disabled_is_silent(tmp_path):
    config = _config(tmp_path, enabled=False)
    disp = _FakeDispatcher()
    p_c, p_h = _patch_axes(99.0, 99.0)
    with p_c, p_h:
        await check_memory_and_alert(config, disp)
    assert disp.sent == []


async def test_both_axes_blind_no_alert_no_state(tmp_path):
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    p_c, p_h = _patch_axes(None, None)
    with p_c, p_h:
        await check_memory_and_alert(config, disp)
    assert disp.sent == []
    # must NOT record an OK tier (which would falsely clear a later alert)
    assert not (config.state_path / "memory_alert_state.json").exists()


async def test_host_only_axis_fires_when_container_blind(tmp_path):
    """The reliability point: container read stalls (None) but host axis pages."""
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    p_c, p_h = _patch_axes(None, 96.0)  # host crit
    with p_c, p_h:
        await check_memory_and_alert(config, disp)
    assert len(disp.sent) == 1
    assert disp.sent[0].severity == AlertSeverity.CRITICAL
    # the metric line (first line) carries only the host axis; the blind
    # container axis is omitted (the CRIT advisory sentence below may still
    # mention "container", so assert on the metric line specifically).
    metric_line = disp.sent[0].body.splitlines()[0]
    assert "host 96%" in metric_line
    assert "container" not in metric_line


async def test_status_snapshot_shape(tmp_path):
    config = _config(tmp_path)
    p_c, p_h = _patch_axes(50.0, 60.0)
    with p_c, p_h:
        snap = await memory_status_snapshot(config)
    assert snap["ok"] is True
    assert snap["action"] == "ram-status"
    assert snap["enabled"] is True
    assert snap["tier"] == "ok"
    assert snap["container"]["used_pct"] == 50.0
    assert snap["host"]["used_pct"] == 60.0


# ── flap damping: exit margin, peak baseline, blind-axis hold ───────────────


async def _run(config, disp, readings):
    """Feed (container_pct, host_pct) readings, one tick each."""
    for container_pct, host_pct in readings:
        p_c, p_h = _patch_axes(container_pct, host_pct)
        with p_c, p_h:
            await check_memory_and_alert(config, disp)


async def test_a_reading_hovering_at_the_line_pages_once(tmp_path):
    """Measured 2026-11-14 19:37-19:48Z: the container read 74-78% across the
    75% WARN line and the guardian sent 12 WARN/recovered messages."""
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    hover = [76.0, 74.0, 75.0, 74.0, 76.0, 75.0, 74.0, 76.0, 75.0, 78.0]
    await _run(config, disp, [(v, 54.0) for v in hover])
    assert [a.title for a in disp.sent] == ["RAM WARN"]


async def test_a_dip_and_return_to_the_paged_tier_does_not_repage(tmp_path):
    """Measured 2026-11-14 03:12-03:44Z: five 'tier rose warn→high' pages while
    the reading moved 85-87%: each dip to WARN was recorded and the next rise
    read as new."""
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    await _run(config, disp, [(86.0, 60.0), (78.0, 60.0), (86.0, 60.0), (79.0, 60.0), (87.0, 60.0)])
    assert [a.title for a in disp.sent] == ["RAM HIGH"]


async def test_a_rise_above_the_paged_tier_still_pages_at_once(tmp_path):
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    await _run(config, disp, [(78.0, 10.0), (86.0, 10.0), (93.0, 10.0)])
    assert [a.title for a in disp.sent] == ["RAM WARN", "RAM HIGH", "RAM CRIT"]


async def test_recovery_waits_for_the_margin(tmp_path):
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    await _run(config, disp, [(78.0, 10.0), (72.0, 10.0)])
    assert len(disp.sent) == 1  # 72 is under 75 but not under 75 - 5
    await _run(config, disp, [(69.0, 10.0)])
    assert len(disp.sent) == 2
    assert "recovered" in disp.sent[1].title.lower()


async def test_a_blind_container_read_is_not_a_recovery(tmp_path):
    """Measured 2026-11-14 17:20-17:21Z: a container read that failed for one
    tick left the host axis alone, read as ok, and sent 'recovered'."""
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    await _run(config, disp, [(78.0, 56.0), (None, 56.0), (77.0, 56.0)])
    assert [a.title for a in disp.sent] == ["RAM WARN"]


async def test_old_state_file_seeds_both_axes(tmp_path):
    """A state file written before ``axes`` existed must not read as a fall."""
    config = _config(tmp_path)
    state_file = config.state_path / "memory_alert_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"tier": "high", "last_alert_at": datetime.now(UTC).isoformat()})
    )
    disp = _FakeDispatcher()
    await _run(config, disp, [(82.0, 10.0)])
    assert disp.sent == []  # still HIGH by the margin: no recovery, no re-page
    data = json.loads(state_file.read_text())
    assert data["tier"] == "high" and data["axes"]["container"] == "high"


def _backdate(config, hours):
    state_file = config.state_path / "memory_alert_state.json"
    data = json.loads(state_file.read_text())
    data["last_alert_at"] = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    state_file.write_text(json.dumps(data))


async def test_the_peak_expires_after_one_realert_window(tmp_path):
    """After a CRIT page, a long stretch at WARN still gets its reminder once the
    window passes, and a rise to HIGH after that pages again."""
    config = _config(tmp_path, realert_hours=6.0)
    disp = _FakeDispatcher()
    await _run(config, disp, [(95.0, 10.0), (78.0, 10.0)])
    assert [a.title for a in disp.sent] == ["RAM CRIT"]  # within the window: quiet
    _backdate(config, 7)
    await _run(config, disp, [(78.0, 10.0)])
    assert disp.sent[-1].title == "RAM WARN" and "sustained" in disp.sent[-1].body
    await _run(config, disp, [(86.0, 10.0)])
    assert disp.sent[-1].title == "RAM HIGH"


async def test_a_blind_axis_is_named_in_the_page(tmp_path):
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    await _run(config, disp, [(88.0, 50.0)])
    _backdate(config, 7)
    await _run(config, disp, [(None, 50.0)])
    assert disp.sent[-1].title == "RAM HIGH"
    assert "container unmeasured for 0.0h (holding high)" in disp.sent[-1].body


async def test_a_margin_held_crit_is_not_paged_as_exhaustion(tmp_path):
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    await _run(config, disp, [(95.0, 10.0)])
    _backdate(config, 7)
    await _run(config, disp, [(88.0, 10.0)])  # 88 + 5 >= 92: still held at CRIT
    page = disp.sent[-1]
    assert page.title == "RAM CRIT"
    assert page.severity == AlertSeverity.WARNING
    assert "near exhaustion" not in page.body and "measured high" in page.body


async def test_a_corrupt_state_file_never_disables_the_watch(tmp_path):
    config = _config(tmp_path)
    state_file = config.state_path / "memory_alert_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    for junk in (
        {"tier": ["x"]},
        {"axes": {"container": ["x"]}},
        {"axes": {"host": {}}},
        ["not", "a", "dict"],
    ):
        state_file.write_text(json.dumps(junk))
        disp = _FakeDispatcher()
        await _run(config, disp, [(95.0, 10.0)])
        assert disp.sent and disp.sent[0].title == "RAM CRIT", junk
        assert isinstance(json.loads(state_file.read_text()), dict)  # rewritten


async def test_a_naive_timestamp_in_the_state_file_reads_as_utc(tmp_path):
    config = _config(tmp_path)
    state_file = config.state_path / "memory_alert_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"tier": "warn", "last_alert_at": "2026-11-14T10:00:00"}))
    disp = _FakeDispatcher()
    await _run(config, disp, [(95.0, 10.0)])
    assert [a.title for a in disp.sent] == ["RAM CRIT"]
    assert json.loads(state_file.read_text())["last_alert_at"].endswith("+00:00")


async def test_a_long_blind_hold_stops_setting_the_severity(tmp_path):
    """A container that stays unreadable after a CRIT page still holds CRIT, but
    after one window it no longer pages as CRITICAL 'near exhaustion'."""
    config = _config(tmp_path, realert_hours=6.0)
    disp = _FakeDispatcher()
    await _run(config, disp, [(95.0, 50.0), (None, 50.0)])
    state_file = config.state_path / "memory_alert_state.json"
    data = json.loads(state_file.read_text())
    assert "container" in data["blind_since"]
    stale = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    data["blind_since"]["container"] = stale
    data["last_alert_at"] = stale
    state_file.write_text(json.dumps(data))
    await _run(config, disp, [(None, 50.0)])
    page = disp.sent[-1]
    assert page.title == "RAM CRIT"
    assert page.severity == AlertSeverity.WARNING
    assert "near exhaustion" not in page.body
    assert "container unmeasured for 7.0h (holding crit)" in page.body
    await _run(config, disp, [(60.0, 50.0)])  # read again: the clock clears
    assert "container" not in json.loads(state_file.read_text())["blind_since"]


async def test_a_non_numeric_margin_falls_back(tmp_path):
    config = _config(tmp_path, exit_margin_pct=None)
    disp = _FakeDispatcher()
    await _run(config, disp, [(78.0, 10.0), (72.0, 10.0)])
    assert [a.title for a in disp.sent] == ["RAM WARN"]  # 5-point default held it


async def test_zero_margin_restores_the_old_crossing_behaviour(tmp_path):
    config = _config(tmp_path, exit_margin_pct=0.0)
    disp = _FakeDispatcher()
    await _run(config, disp, [(76.0, 10.0), (74.0, 10.0)])
    assert [a.title for a in disp.sent] == ["RAM WARN", "RAM pressure recovered"]


# ── host-safe import isolation (F.4 minimal-venv lesson) ─────────────────────


def test_memory_watch_imports_host_safe():
    """The guardian runs a minimal venv WITHOUT aiohttp. memory_watch executes
    host-side, so importing it must NOT pull genesis.observability / aiohttp.
    Runs in a fresh interpreter to assert real import isolation."""
    code = (
        "import sys; import genesis.guardian.memory_watch;"
        "assert 'aiohttp' not in sys.modules, 'aiohttp leaked';"
        "assert 'genesis.observability' not in sys.modules, 'observability leaked';"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"import isolation failed: {result.stderr}"
    assert "ok" in result.stdout
