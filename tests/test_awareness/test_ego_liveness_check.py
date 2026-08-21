"""Tests for the ego-cycle liveness awareness alert (_check_ego_liveness).

The pure verdict is tested in tests/ego/test_liveness.py; here the ALERTING
state machine is under test — one self-superseding 'high' observation per ego,
gated/paused suppression, and resolve-on-recovery — driven off a seeded
job_health.last_success and a mocked runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.awareness import loop
from genesis.db.schema import create_all_tables

NOW = datetime.now(UTC)
USER_SRC = "ego_liveness:user_ego_cycle"
GEN_SRC = "ego_liveness:genesis_ego_cycle"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _mgr(interval=90, paused=False):
    return SimpleNamespace(current_interval_minutes=interval, is_paused=paused)


def _wire(monkeypatch, *, user_mgr=None, genesis_mgr=None, gate_on=True):
    from genesis import runtime as rt_mod
    from genesis.autonomy import cli_policy

    fake_rt = SimpleNamespace(
        _ego_cadence_manager=user_mgr,
        _genesis_ego_cadence_manager=genesis_mgr,
    )
    monkeypatch.setattr(rt_mod.GenesisRuntime, "instance", lambda: fake_rt)
    monkeypatch.setattr(
        cli_policy,
        "load_autonomous_cli_policy",
        lambda *a, **k: cli_policy.AutonomousCliPolicy(
            manual_approval_required=gate_on,
        ),
    )


async def _seed_last_success(db, job_name, minutes_ago):
    iso = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    await db.execute(
        "INSERT INTO job_health (job_name, last_run, last_success, updated_at) VALUES (?, ?, ?, ?)",
        (job_name, iso, iso, iso),
    )
    await db.commit()


async def _seed_pending_approval(db, policy_id):
    await db.execute(
        "INSERT INTO approval_requests "
        "(id, action_type, action_class, description, context, status) "
        "VALUES (?, 'autonomous_cli_fallback', 'costly_reversible', 'x', ?, 'pending')",
        (f"ap-{policy_id}", f'{{"policy_id": "{policy_id}", "subsystem": "ego"}}'),
    )
    await db.commit()


async def _open_count(db, source):
    cur = await db.execute(
        "SELECT COUNT(*) FROM observations WHERE source=? AND type='ego_alert' AND resolved=0",
        (source,),
    )
    return (await cur.fetchone())[0]


def test_ego_alert_type_is_registered():
    """The ego_alert observation type must have an explicit TTL — an
    unregistered type logs a warning per alert and inherits the 14d default."""
    from genesis.db.crud.observations import _TTL_BY_TYPE

    assert loop._EGO_LIVENESS_TYPE in _TTL_BY_TYPE


@pytest.mark.asyncio
async def test_stalled_ego_raises_alert(db, monkeypatch):
    """User ego silent 3 days on a 90m cadence, gate off → one alert; the
    genesis ego (recent) stays clear."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=_mgr(), gate_on=False)
    await _seed_last_success(db, "user_ego_cycle", 3 * 24 * 60)
    await _seed_last_success(db, "genesis_ego_cycle", 20)

    await loop._check_ego_liveness(db)

    assert await _open_count(db, USER_SRC) == 1
    assert await _open_count(db, GEN_SRC) == 0


@pytest.mark.asyncio
async def test_recent_ego_no_alert(db, monkeypatch):
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=_mgr(), gate_on=False)
    await _seed_last_success(db, "user_ego_cycle", 30)
    await _seed_last_success(db, "genesis_ego_cycle", 30)

    await loop._check_ego_liveness(db)

    assert await _open_count(db, USER_SRC) == 0
    assert await _open_count(db, GEN_SRC) == 0


@pytest.mark.asyncio
async def test_gated_ego_not_alerted(db, monkeypatch):
    """A long-silent ego that is GATED on a pending approval (gate ON) is a
    legitimate wait, not a stall — no alert."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=None, gate_on=True)
    await _seed_last_success(db, "user_ego_cycle", 3 * 24 * 60)
    await _seed_pending_approval(db, "user_ego_cycle")

    await loop._check_ego_liveness(db)

    assert await _open_count(db, USER_SRC) == 0


@pytest.mark.asyncio
async def test_idempotent_then_resolves_on_recovery(db, monkeypatch):
    """Repeated ticks while stalled keep exactly one open alert; once a cycle
    lands (recent last_success) the alert auto-resolves."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=None, gate_on=False)
    await _seed_last_success(db, "user_ego_cycle", 3 * 24 * 60)

    await loop._check_ego_liveness(db)
    await loop._check_ego_liveness(db)  # second tick must not duplicate
    assert await _open_count(db, USER_SRC) == 1

    # Ego recovers: advance last_success to now.
    await db.execute(
        "UPDATE job_health SET last_success=? WHERE job_name='user_ego_cycle'",
        (NOW.isoformat(),),
    )
    await db.commit()
    await loop._check_ego_liveness(db)
    assert await _open_count(db, USER_SRC) == 0
