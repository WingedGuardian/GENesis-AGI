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

    def set_scan(mbs, *, raises=False, uncertain_pids=()):
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
                uncertain=[f"{p} (unreadable)" for p in uncertain_pids],
                uncertain_pids=set(uncertain_pids),
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


def _expire(monkeypatch):
    stamp, threshold, reading = errors._process_cache
    monkeypatch.setattr(errors, "_process_cache", (stamp - errors._PROCESS_TTL_S, threshold, reading))


async def _state() -> str:
    """'firing', 'unknown' (absent but in the unknown-id channel) or 'clear'."""
    unknown: set[str] = set()
    alerts, ids = await errors._compute_alerts(queue_unknown_ids=unknown)
    if _ALARM in ids:
        assert any(a["id"] == _ALARM for a in alerts)
        return "firing"
    return "unknown" if _ALARM in unknown else "clear"


def _rescan(monkeypatch, set_scan, *args, **kwargs):
    """Swap the scan but keep the cached reading, expired so the next call refreshes."""
    saved = errors._process_cache
    set_scan(*args, **kwargs)
    monkeypatch.setattr(errors, "_process_cache", saved)
    _expire(monkeypatch)


@pytest.mark.asyncio
async def test_scan_is_cached_for_a_minute(health, monkeypatch):
    set_scan, calls = health
    set_scan([9000])
    assert await _alarm() is not None
    assert await _alarm() is not None
    assert len(calls) == 1
    _expire(monkeypatch)
    await _alarm()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_new_threshold_is_never_served_from_the_cache(health, monkeypatch):
    """CodeRabbit: a target found at 6144 MB says nothing about 12288 MB; the
    scan filters by the threshold, so a changed threshold scans again."""
    set_scan, calls = health
    set_scan([7168])
    assert await _alarm() is not None
    monkeypatch.setattr(process_stop, "load_propose_at_mb", lambda path=None: 12288)
    await _alarm()
    assert calls == [6144, 12288]


@pytest.mark.asyncio
async def test_a_raising_scan_logs_once_and_emits_nothing(health, caplog, monkeypatch):
    set_scan, _ = health
    set_scan([], raises=True)
    with caplog.at_level(logging.ERROR, logger="genesis.mcp.health.errors"):
        assert await _alarm() is None
        monkeypatch.setattr(errors, "_process_cache", None)
        assert await _alarm() is None
    assert caplog.text.count("process_memory_high: the stop tool's scan raised") == 1


@pytest.mark.asyncio
async def test_a_failed_refresh_reads_unknown_not_cleared(health, monkeypatch):
    set_scan, _ = health
    set_scan([9000])
    assert await _state() == "firing"
    _rescan(monkeypatch, set_scan, [], raises=True)
    assert await _state() == "unknown"


@pytest.mark.asyncio
async def test_an_unreadable_candidate_with_no_target_is_unknown(health, monkeypatch):
    """An unreadable process may be the one over the line."""
    set_scan, _ = health
    set_scan([9000])  # pid 300
    assert await _state() == "firing"
    _rescan(monkeypatch, set_scan, [], uncertain_pids=[300])
    assert await _state() == "unknown"


@pytest.mark.asyncio
async def test_a_readable_target_fires_while_another_is_unreadable(health):
    set_scan, _ = health
    set_scan([9000], uncertain_pids=[999])
    assert await _state() == "firing"


@pytest.mark.asyncio
async def test_an_unreadable_process_is_not_lost_behind_another_target(health, monkeypatch):
    """CodeRabbit: A flagged, then A unreadable while B is a readable target,
    then B under the line while A stays unreadable. Nothing retained A, and
    nothing needs to: the last reading is unknown, never clear."""
    set_scan, _ = health
    set_scan([9000])  # A = pid 300
    assert await _state() == "firing"
    _rescan(monkeypatch, set_scan, [8000], uncertain_pids=[999])  # B readable, A unreadable
    assert await _state() == "firing"
    _rescan(monkeypatch, set_scan, [], uncertain_pids=[999])  # B eased, A still unreadable
    assert await _state() == "unknown"


@pytest.mark.asyncio
async def test_a_blind_reading_logs_once_an_hour(health, caplog, monkeypatch):
    set_scan, _ = health
    with caplog.at_level(logging.ERROR, logger="genesis.mcp.health.errors"):
        set_scan([], uncertain_pids=[999])
        assert await _state() == "unknown"
        _rescan(monkeypatch, set_scan, [], uncertain_pids=[999])
        assert await _state() == "unknown"
    assert caplog.text.count("the alarm may be blind") == 1


@pytest.mark.asyncio
async def test_a_complete_reading_with_no_target_clears(health, monkeypatch):
    """Control: nothing unreadable and nothing over the line is a real clearance."""
    set_scan, _ = health
    set_scan([9000])
    assert await _state() == "firing"
    _rescan(monkeypatch, set_scan, [])
    assert await _state() == "clear"


@pytest.mark.asyncio
async def test_overlapping_callers_share_one_scan(health):
    import asyncio

    set_scan, calls = health
    set_scan([9000])
    results = await asyncio.gather(*(errors._process_memory_high() for _ in range(5)))
    assert all(isinstance(r, tuple) for r in results)
    assert len(calls) == 1


def test_tier_2_and_scoped():
    assert REMEDIATION_MAP[_ALARM] == frozenset({"container.process_control"})
    alarms = classify_alerts(
        [{"id": _ALARM, "severity": "WARNING", "message": "m"}], available_tools()
    )
    assert [(a.alert_id, a.tier) for a in alarms] == [(_ALARM, 2)]
