"""``infra:process_memory_high``: one runaway process over process_stop.propose_at_mb.

The alarm reads the stop tool's own ``scan()``, so it never names a process
the remedy would not stop, and it wakes the Sentinel at tier 2.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.mcp import health_mcp
from genesis.mcp.health import errors
from genesis.observability.provider_activity import ProviderActivityTracker
from genesis.sentinel import process_stop
from genesis.sentinel.classifier import classify_alerts
from genesis.sentinel.process_stop import Scan, Target
from genesis.sentinel.remediation_map import REMEDIATION_MAP, available_tools

_ALARM = "infra:process_memory_high"


@pytest.fixture()
def health(monkeypatch):
    monkeypatch.setattr(health_mcp, "_activity_tracker", ProviderActivityTracker())
    service = MagicMock()
    service.snapshot = AsyncMock(
        return_value={
            "call_sites": {},
            "queues": {},
            "cc_sessions": {},
            "infrastructure": {},
            "services": {},
            "awareness": {},
        }
    )
    monkeypatch.setattr(health_mcp, "_service", service)
    monkeypatch.setattr(process_stop, "load_mode", lambda path=None: process_stop.MODE_PROPOSE_ONLY)
    monkeypatch.setattr(process_stop, "load_propose_at_mb", lambda path=None: 6144)
    calls = []

    def set_scan(mbs, *, raises=False):
        def fake(threshold):
            calls.append(threshold)
            if raises:
                raise OSError("proc gone")
            return Scan(
                targets=[
                    Target(
                        pid=300 + i,
                        starttime=1,
                        label="codex",
                        held_kb=mb * 1024,
                        unit="session-1.scope",
                    )
                    for i, mb in enumerate(mbs)
                ],
                threshold_mb=threshold,
            )

        monkeypatch.setattr(errors, "_process_scan", fake)
        monkeypatch.setattr(errors, "_process_cache", None)

    return set_scan, calls


async def _alarm():
    alerts, ids = await errors._compute_alerts()
    hits = [a for a in alerts if a["id"] == _ALARM]
    assert (_ALARM in ids) == bool(hits)
    return hits[0] if hits else None


@pytest.mark.asyncio
async def test_fires_and_names_the_largest(health):
    set_scan, calls = health
    set_scan([11968, 7000])
    alarm = await _alarm()
    assert alarm is not None and alarm["severity"] == "WARNING"
    assert "2 process(es)" in alarm["message"] and "pid 300 codex at 11968 MB" in alarm["message"]
    assert "process_stop" in alarm["message"]
    assert calls == [6144]


@pytest.mark.asyncio
async def test_silent_with_no_target(health):
    set_scan, _ = health
    set_scan([])
    assert await _alarm() is None


@pytest.mark.asyncio
async def test_lever_or_threshold_off_never_scans(health, monkeypatch):
    set_scan, calls = health
    set_scan([9000])
    monkeypatch.setattr(process_stop, "load_mode", lambda path=None: process_stop.MODE_OFF)
    assert await _alarm() is None
    monkeypatch.setattr(process_stop, "load_mode", lambda path=None: process_stop.MODE_PROPOSE_ONLY)
    monkeypatch.setattr(process_stop, "load_propose_at_mb", lambda path=None: None)
    assert await _alarm() is None
    assert calls == []


@pytest.mark.asyncio
async def test_scan_is_cached_for_a_minute(health, monkeypatch):
    set_scan, calls = health
    set_scan([9000])
    assert await _alarm() is not None
    assert await _alarm() is not None
    assert len(calls) == 1
    stamp, held = errors._process_cache
    monkeypatch.setattr(errors, "_process_cache", (stamp - errors._PROCESS_TTL_S, held))
    await _alarm()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_raising_scan_logs_once_and_emits_nothing(health, caplog, monkeypatch):
    set_scan, _ = health
    set_scan([], raises=True)
    with caplog.at_level(logging.ERROR, logger="genesis.mcp.health.errors"):
        assert await _alarm() is None
        monkeypatch.setattr(errors, "_process_cache", None)
        assert await _alarm() is None
    assert caplog.text.count("process_memory_high: the stop tool's scan raised") == 1


def test_tier_2_and_scoped():
    assert REMEDIATION_MAP[_ALARM] == frozenset({"container.process_control"})
    alarms = classify_alerts(
        [{"id": _ALARM, "severity": "WARNING", "message": "m"}], available_tools()
    )
    assert [(a.alert_id, a.tier) for a in alarms] == [(_ALARM, 2)]
