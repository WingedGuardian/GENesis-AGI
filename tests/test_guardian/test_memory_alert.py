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
