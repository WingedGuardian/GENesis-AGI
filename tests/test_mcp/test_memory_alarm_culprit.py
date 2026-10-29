"""The container memory alarm names the process trees holding the memory.

``infra:container_memory_high`` used to say only THAT memory was high. It now
carries the top process trees by summed RSS, taken once when the alarm begins
firing (``_compute_alerts`` runs every tick and every dashboard poll), reused
while it keeps firing, and taken afresh after it stops and fires again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.mcp import health_mcp
from genesis.mcp.health import errors
from genesis.observability import pid_breakdown
from genesis.observability.pid_breakdown import memory_summary
from genesis.observability.provider_activity import ProviderActivityTracker

_ALARM = "infra:container_memory_high"


@pytest.fixture()
def health(monkeypatch):
    monkeypatch.setattr(errors, "_memory_culprits", None)
    monkeypatch.setattr(health_mcp, "_activity_tracker", ProviderActivityTracker())
    service = MagicMock()
    monkeypatch.setattr(health_mcp, "_service", service)
    calls = []

    def fake_summary(*_a, **_k):
        calls.append(1)
        return f"Top memory trees at 00:00Z (summed RSS): codex (pid 100): 13.0 GB, 3 children. #{len(calls)}"

    monkeypatch.setattr(pid_breakdown, "memory_summary", fake_summary)

    def set_pct(pct, **extra):
        service.snapshot = AsyncMock(
            return_value={
                "call_sites": {},
                "queues": {},
                "cc_sessions": {},
                "infrastructure": {
                    "container_memory": {"anon_pct": pct, "current_gb": 28, "limit_gb": 32, **extra}
                },
                "services": {},
                "awareness": {},
            }
        )

    return set_pct, calls


async def _memory_message():
    alerts, _ids = await errors._compute_alerts()
    mem = [a for a in alerts if a["id"] == _ALARM]
    return mem[0]["message"] if mem else None


@pytest.mark.asyncio
async def test_alarm_message_names_the_top_tree(health):
    set_pct, calls = health
    set_pct(87)
    message = await _memory_message()
    assert message.startswith("Container memory at 87% anon+kernel (28/32GB total). ")
    assert "codex (pid 100): 13.0 GB, 3 children" in message
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_breakdown_is_taken_once_per_firing_episode(health):
    set_pct, calls = health
    set_pct(87)
    first = await _memory_message()
    set_pct(88)
    second = await _memory_message()
    assert len(calls) == 1  # still firing: reused, no second /proc pass
    assert first.endswith("#1") and second.endswith("#1")

    set_pct(84)
    assert await _memory_message() is None  # stopped firing: the held line is dropped
    set_pct(87)
    third = await _memory_message()
    assert len(calls) == 2  # a new episode takes a fresh breakdown
    assert third.endswith("#2")


@pytest.mark.asyncio
async def test_cached_breakdown_retains_utc_sample_date_across_days(health, monkeypatch):
    set_pct, _calls = health
    sampled_at = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    samples = []

    class Clock:
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            return sampled_at

    def sample(*_a, **_k):
        samples.append(sampled_at)
        return {
            "status": "sampled",
            "trees": [{"label": "synthetic", "root_pid": 100, "rss_mb": 50, "children": 0}],
            "rss_unreadable": 0,
            "processes_seen": 1,
            "truncated": [],
        }

    monkeypatch.setattr(pid_breakdown, "datetime", Clock)
    monkeypatch.setattr(pid_breakdown, "memory_trees", sample)
    monkeypatch.setattr(pid_breakdown, "memory_summary", memory_summary)
    monkeypatch.delenv("GENESIS_PID_BREAKDOWN_DISABLED", raising=False)
    set_pct(87)
    first = await _memory_message()
    assert "Top memory trees at 2026-12-31 23:59Z" in first

    sampled_at += timedelta(days=2)
    set_pct(88)
    held = await _memory_message()
    assert held.startswith("Container memory at 88%")
    assert "Top memory trees at 2026-12-31 23:59Z" in held
    assert "2027-01-02" not in held
    assert len(samples) == 1

    set_pct(84)
    assert await _memory_message() is None
    set_pct(87)
    refreshed = await _memory_message()
    assert "Top memory trees at 2027-01-02 23:59Z" in refreshed
    assert len(samples) == 2


@pytest.mark.asyncio
async def test_alarm_message_says_how_much_is_kernel(health):
    """Kernel memory counts toward the alarm but no process holds it."""
    set_pct, _calls = health
    set_pct(87, anon_gb=10.5, kernel_gb=9.24)
    message = await _memory_message()
    assert (
        "(28/32GB total; anon 10.5 GB, kernel 9.24 GB (kernel is held by no process)). " in message
    )


@pytest.mark.asyncio
async def test_breakdown_failure_is_stated_not_silent(health, monkeypatch):
    set_pct, _calls = health

    def boom(*_a, **_k):
        raise OSError("no /proc")

    monkeypatch.setattr(pid_breakdown, "memory_summary", boom)
    set_pct(87)
    message = await _memory_message()
    assert message.endswith("Process breakdown unavailable: OSError.")
