"""``infra:codex_mcp_held``: MCP servers held by a shared Codex app-server.

The alarm fires when the stop tool's own scan totals at least
``codex_mcp_stop.propose_at_mb``, so the Sentinel proposes ``codex_mcp_stop``
before container memory is high (#4302). It reads ``scan()``, so the alarm and
the remedy count the same groups.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.mcp import health_mcp
from genesis.mcp.health import errors
from genesis.observability.provider_activity import ProviderActivityTracker
from genesis.sentinel import codex_mcp_stop
from genesis.sentinel.codex_mcp_stop import Group, Scan

_ALARM = "infra:codex_mcp_held"


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
    monkeypatch.setattr(
        codex_mcp_stop, "load_mode", lambda path=None: codex_mcp_stop.MODE_PROPOSE_ONLY
    )
    monkeypatch.setattr(codex_mcp_stop, "load_propose_at_mb", lambda path=None: 4096)
    calls = []

    def set_scan(groups_mb, *, app_servers=(1,), uncertain=(), raises=False, reset_cache=True):
        def fake():
            calls.append(1)
            if raises:
                raise OSError("proc gone")
            return Scan(
                app_servers=list(app_servers),
                groups=[
                    Group(pgid=100 + i, starttime=1, cmd="python", rss_kb=mb * 1024)
                    for i, mb in enumerate(groups_mb)
                ],
                uncertain=list(uncertain),
            )

        monkeypatch.setattr(errors, "_codex_mcp_scan", fake)
        if reset_cache:
            monkeypatch.setattr(errors, "_codex_mcp_cache", None)

    return set_scan, calls


async def _alarm():
    alerts, ids = await errors._compute_alerts()
    hits = [a for a in alerts if a["id"] == _ALARM]
    assert (_ALARM in ids) == bool(hits)
    return hits[0] if hits else None


@pytest.mark.asyncio
async def test_fires_at_the_threshold_as_a_warning(health):
    set_scan, _ = health
    set_scan([2048, 2048])
    alarm = await _alarm()
    assert alarm is not None and alarm["severity"] == "WARNING"
    assert "2 MCP server group(s), 4096 MB" in alarm["message"]
    assert "codex_mcp_stop" in alarm["message"]


@pytest.mark.asyncio
async def test_silent_just_below_the_threshold(health):
    set_scan, _ = health
    set_scan([2048, 2047])
    assert await _alarm() is None


@pytest.mark.asyncio
async def test_silent_with_no_app_server_groups(health):
    set_scan, _ = health
    set_scan([], app_servers=())
    assert await _alarm() is None


@pytest.mark.asyncio
async def test_lever_off_never_scans(health, monkeypatch):
    set_scan, calls = health
    set_scan([5000])
    monkeypatch.setattr(codex_mcp_stop, "load_mode", lambda path=None: codex_mcp_stop.MODE_OFF)
    assert await _alarm() is None
    assert calls == []


@pytest.mark.asyncio
async def test_threshold_off_never_scans(health, monkeypatch):
    set_scan, calls = health
    set_scan([5000])
    monkeypatch.setattr(codex_mcp_stop, "load_propose_at_mb", lambda path=None: None)
    assert await _alarm() is None
    assert calls == []


@pytest.mark.asyncio
async def test_scan_is_cached_for_a_minute(health, monkeypatch):
    set_scan, calls = health
    set_scan([5000])
    assert await _alarm() is not None
    assert await _alarm() is not None
    assert len(calls) == 1, "second call inside the TTL reuses the scan"
    stamp, held = errors._codex_mcp_cache
    monkeypatch.setattr(errors, "_codex_mcp_cache", (stamp - errors._CODEX_MCP_TTL_S, held))
    await _alarm()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_raising_scan_logs_once_and_emits_nothing(health, caplog, monkeypatch):
    set_scan, _ = health
    set_scan([], raises=True)
    with caplog.at_level(logging.ERROR, logger="genesis.mcp.health.errors"):
        assert await _alarm() is None
        monkeypatch.setattr(errors, "_codex_mcp_cache", None)
        assert await _alarm() is None
    assert caplog.text.count("scan raised") == 1


async def _state() -> str:
    """'firing', 'unknown' (absent but in the unknown-id channel) or 'clear'."""
    unknown: set[str] = set()
    alerts, ids = await errors._compute_alerts(queue_unknown_ids=unknown)
    if _ALARM in ids:
        assert any(a["id"] == _ALARM for a in alerts)
        return "firing"
    return "unknown" if _ALARM in unknown else "clear"


def _expire(monkeypatch):
    stamp, reading = errors._codex_mcp_cache
    monkeypatch.setattr(errors, "_codex_mcp_cache", (stamp - errors._CODEX_MCP_TTL_S, reading))


@pytest.mark.asyncio
async def test_a_failed_refresh_reads_unknown_not_cleared(health, monkeypatch):
    """A read error is no evidence the groups are gone: the alarm is unknown,
    so neither the durable row nor a pending approval reads it as cleared."""
    set_scan, _ = health
    set_scan([5000])
    assert await _state() == "firing"
    _expire(monkeypatch)
    set_scan([], raises=True, reset_cache=False)
    assert await _state() == "unknown"


@pytest.mark.asyncio
async def test_an_all_uncertain_refresh_reads_unknown(health, monkeypatch):
    set_scan, _ = health
    set_scan([5000])
    assert await _state() == "firing"
    _expire(monkeypatch)
    set_scan([], uncertain=["777 python (descriptors unreadable)"], reset_cache=False)
    assert await _state() == "unknown"


@pytest.mark.asyncio
async def test_a_blind_first_scan_after_a_restart_is_unknown(health):
    """Audit F3: no earlier reading in this process, and the first scan is
    blind. That must not read as cleared."""
    set_scan, _ = health
    set_scan([], uncertain=["101 python (descriptors unreadable)"])
    assert await _state() == "unknown"


@pytest.mark.asyncio
async def test_a_partial_refresh_below_the_threshold_is_unknown(health, monkeypatch):
    """Two app-servers, one now unreadable: the readable one's small group is a
    lower bound, not evidence the other's memory was released."""
    set_scan, _ = health
    set_scan([5000], app_servers=(1, 2))
    assert await _state() == "firing"
    _expire(monkeypatch)
    set_scan(
        [512],
        app_servers=(1, 2),
        uncertain=["app-server 2 (descriptors unreadable)"],
        reset_cache=False,
    )
    assert await _state() == "unknown"


@pytest.mark.asyncio
async def test_a_partial_refresh_past_the_threshold_fires(health):
    """A lower bound past the threshold is proof on its own."""
    set_scan, _ = health
    set_scan([5000], app_servers=(1, 2), uncertain=["app-server 2 (descriptors unreadable)"])
    assert await _state() == "firing"


@pytest.mark.asyncio
async def test_no_old_figure_outlives_a_complete_reading(health, monkeypatch):
    """Audit F2: nothing is retained. Unknown for as long as the scan is
    partial, then the first complete reading decides, here a clear one."""
    set_scan, _ = health
    set_scan([5000], app_servers=(1, 2))
    assert await _state() == "firing"
    for _ in range(3):
        _expire(monkeypatch)
        set_scan(
            [100],
            app_servers=(1, 2),
            uncertain=["app-server 2 (descriptors unreadable)"],
            reset_cache=False,
        )
        assert await _state() == "unknown"
    _expire(monkeypatch)
    set_scan([100], app_servers=(1, 2), reset_cache=False)
    assert await _state() == "clear"


@pytest.mark.asyncio
async def test_a_complete_refresh_below_the_threshold_clears(health, monkeypatch):
    """Control: with nothing unreadable, a lower reading is a real clearance."""
    set_scan, _ = health
    set_scan([5000])
    assert await _state() == "firing"
    _expire(monkeypatch)
    set_scan([512], reset_cache=False)
    assert await _state() == "clear"


@pytest.mark.asyncio
async def test_the_read_path_reports_the_unknown_id(health):
    """The Sentinel reads `_impl_health_alerts`: the unknown id must reach it."""
    set_scan, _ = health
    set_scan([], uncertain=["101 python (descriptors unreadable)"])
    unknown: set[str] = set()
    alerts = await errors._impl_health_alerts(active_only=True, unknown_ids=unknown)
    assert _ALARM in unknown
    assert not any(a["id"] == _ALARM for a in alerts)


@pytest.mark.asyncio
async def test_overlapping_callers_share_one_scan(health):
    import asyncio

    set_scan, calls = health
    set_scan([5000])
    results = await asyncio.gather(*(errors._codex_mcp_held() for _ in range(5)))
    assert all(r is not None for r in results)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unclassifiable_children_with_no_group_log_blindness(health, caplog):
    set_scan, _ = health
    set_scan([], uncertain=["101 python (descriptors unreadable)"])
    with caplog.at_level(logging.ERROR, logger="genesis.mcp.health.errors"):
        assert await _alarm() is None
    assert "may be blind" in caplog.text


# --- the threshold loader ----------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ("codex_mcp_stop:\n  mode: propose_only\n", codex_mcp_stop.DEFAULT_PROPOSE_AT_MB),
        ("", codex_mcp_stop.DEFAULT_PROPOSE_AT_MB),
        ("codex_mcp_stop:\n  propose_at_mb: 2048\n", 2048),
        ("codex_mcp_stop:\n  propose_at_mb: 0\n", None),
        ("codex_mcp_stop:\n  propose_at_mb: -5\n", None),
        ("codex_mcp_stop:\n  propose_at_mb: lots\n", None),
        ("codex_mcp_stop:\n  propose_at_mb: true\n", None),
        ("codex_mcp_stop: [\n", None),
    ],
)
def test_load_propose_at_mb(tmp_path, body, expected):
    cfg = tmp_path / "sentinel.yaml"
    cfg.write_text(body)
    assert codex_mcp_stop.load_propose_at_mb(cfg) == expected


def test_a_bad_threshold_warns_once(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(codex_mcp_stop, "_WARNED_ONCE", set())
    cfg = tmp_path / "sentinel.yaml"
    cfg.write_text("codex_mcp_stop:\n  propose_at_mb: -1\n")
    with caplog.at_level(logging.WARNING, logger=codex_mcp_stop.logger.name):
        for _ in range(5):
            assert codex_mcp_stop.load_propose_at_mb(cfg) is None
    assert caplog.text.count("not a positive integer") == 1


def test_a_broken_config_warns_once_across_mode_reads(tmp_path, caplog, monkeypatch):
    """load_mode runs first on every health computation; a broken overlay must
    not log a traceback every time."""
    monkeypatch.setattr(codex_mcp_stop, "_WARNED_ONCE", set())
    cfg = tmp_path / "sentinel.yaml"
    cfg.write_text("codex_mcp_stop: [\n")
    with caplog.at_level(logging.WARNING, logger=codex_mcp_stop.logger.name):
        for _ in range(5):
            assert codex_mcp_stop.load_mode(cfg) == codex_mcp_stop.MODE_OFF
    assert caplog.text.count("Failed to load") == 1


def test_local_overlay_overrides_the_threshold(tmp_path):
    cfg = tmp_path / "sentinel.yaml"
    cfg.write_text("codex_mcp_stop:\n  propose_at_mb: 4096\n")
    (tmp_path / "sentinel.local.yaml").write_text("codex_mcp_stop:\n  propose_at_mb: 1024\n")
    assert codex_mcp_stop.load_propose_at_mb(cfg) == 1024
