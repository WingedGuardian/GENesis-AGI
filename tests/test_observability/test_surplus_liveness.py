"""Tests for the truthful liveness fields on the surplus snapshot.

surplus_status() now exposes stalled / last_success_at / stall_reason / liveness_error
from job_health["surplus_dispatch"] via compute_pulse_liveness, so a WEDGED surplus
scheduler stops reading green (the idle-proxy `status` hid it). Fail-LOUD: any read
error, or an inability to determine the pause state (no live runtime), yields
liveness_error=True (→ dashboard `unknown`), never a defaulted-False that reads green.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.observability.snapshots.surplus import surplus_status
from genesis.runtime._core import GenesisRuntime

NOW = datetime.now(UTC)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def runtime_singleton():
    original = GenesisRuntime._instance
    yield
    GenesisRuntime._instance = original


def _install_runtime(*, paused=False, present=True):
    GenesisRuntime._instance = (
        SimpleNamespace(paused=paused, idle_detector=None) if present else None
    )


class _FakeQueue:
    async def pending_count(self):
        return 0


def _fake_surplus(is_idle=True):
    return SimpleNamespace(
        _dispatch_interval=5,
        _idle_detector=SimpleNamespace(is_idle=lambda: is_idle),
        _queue=_FakeQueue(),
    )


async def _seed_last_success(db, *, minutes_ago):
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    await db.execute(
        "INSERT INTO job_health (job_name, last_run, last_success, updated_at) VALUES (?, ?, ?, ?)",
        ("surplus_dispatch", ts, ts, ts),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_stale_surplus_not_paused_reports_stalled(db, runtime_singleton):
    """No completed dispatch in 5h, not paused → stalled=True with a reason."""
    _install_runtime(paused=False)
    await _seed_last_success(db, minutes_ago=5 * 60)
    result = await surplus_status(db, _fake_surplus())
    assert result["stalled"] is True
    assert result["liveness_error"] is False
    assert result["stall_reason"]
    assert result["last_success_at"] is not None


@pytest.mark.asyncio
async def test_recent_surplus_not_stalled(db, runtime_singleton):
    _install_runtime(paused=False)
    await _seed_last_success(db, minutes_ago=5)
    result = await surplus_status(db, _fake_surplus())
    assert result["stalled"] is False
    assert result["liveness_error"] is False


@pytest.mark.asyncio
async def test_paused_surplus_not_stalled(db, runtime_singleton):
    """A globally-paused surplus legitimately freezes last_success → never a stall."""
    _install_runtime(paused=True)
    await _seed_last_success(db, minutes_ago=5 * 60)
    result = await surplus_status(db, _fake_surplus())
    assert result["stalled"] is False
    assert result["liveness_error"] is False


@pytest.mark.asyncio
async def test_no_runtime_reports_liveness_error(db, runtime_singleton):
    """The standalone-MCP hole: db present but NO live runtime (can't read pause) →
    liveness_error, never a defaulted-False that reads green while wedged."""
    _install_runtime(present=False)
    await _seed_last_success(db, minutes_ago=5 * 60)
    # surplus=None mirrors the standalone_health.py:198 call.
    result = await surplus_status(db, None)
    assert result["liveness_error"] is True
    assert result["stalled"] is False


@pytest.mark.asyncio
async def test_never_succeeded_not_stalled(db, runtime_singleton):
    """No job_health row (never once succeeded / fresh install) → not stalled and
    NOT a liveness_error (the job_never_succeeded alarm owns that case)."""
    _install_runtime(paused=False)
    result = await surplus_status(db, _fake_surplus())
    assert result["stalled"] is False
    assert result["liveness_error"] is False
    assert result["last_success_at"] is None
