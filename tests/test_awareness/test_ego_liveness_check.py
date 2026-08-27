"""Tests for the ego-cycle liveness awareness alert (_check_ego_liveness).

The pure verdict is tested in tests/ego/test_liveness.py; here the ALERTING
state machine is under test — one self-superseding 'high' observation per ego,
suppression (no recent intent / gated), disabled-ego resolution, and
resolve-on-recovery — driven off seeded job_health.last_success + ego_state
last_proactive_fire and a mocked runtime.
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


def _mgr(interval=90):
    return SimpleNamespace(current_interval_minutes=interval)


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


async def _seed(db, job_name, *, success_min_ago, intent_min_ago=None):
    s = (NOW - timedelta(minutes=success_min_ago)).isoformat()
    await db.execute(
        "INSERT INTO job_health (job_name, last_run, last_success, updated_at) VALUES (?, ?, ?, ?)",
        (job_name, s, s, s),
    )
    if intent_min_ago is not None:
        i = (NOW - timedelta(minutes=intent_min_ago)).isoformat()
        await db.execute(
            "INSERT INTO ego_state (key, value, updated_at) VALUES (?, ?, ?)",
            (f"last_proactive_fire:{job_name}", i, i),
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
    """User ego actively pushing (recent intent) but no completion in 3 days,
    gate off → one alert; the genesis ego (recent completion) stays clear."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=_mgr(), gate_on=False)
    await _seed(db, "user_ego_cycle", success_min_ago=3 * 24 * 60, intent_min_ago=5)
    await _seed(db, "genesis_ego_cycle", success_min_ago=20, intent_min_ago=25)

    await loop._check_ego_liveness(db)

    assert await _open_count(db, USER_SRC) == 1
    assert await _open_count(db, GEN_SRC) == 0


@pytest.mark.asyncio
async def test_suppressed_ego_no_recent_intent_no_alert(db, monkeypatch):
    """The convergence case: 3 days since a completed cycle, but the ego also
    stopped TRYING (intent is old too — idle/quiet/paused/global-pause suppressed
    the push). Lag small → no alert, with no per-condition enumeration."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=None, gate_on=False)
    await _seed(
        db,
        "user_ego_cycle",
        success_min_ago=3 * 24 * 60 + 20,
        intent_min_ago=3 * 24 * 60,
    )

    await loop._check_ego_liveness(db)

    assert await _open_count(db, USER_SRC) == 0


@pytest.mark.asyncio
async def test_disabled_ego_resolves_open_alert(db, monkeypatch):
    """An ego that was stalled and is then disabled (manager gone) must clear
    its standing alert, not leave it open forever."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=None, gate_on=False)
    await _seed(db, "user_ego_cycle", success_min_ago=3 * 24 * 60, intent_min_ago=5)
    await loop._check_ego_liveness(db)
    assert await _open_count(db, USER_SRC) == 1

    _wire(monkeypatch, user_mgr=None, genesis_mgr=None, gate_on=False)
    await loop._check_ego_liveness(db)
    assert await _open_count(db, USER_SRC) == 0


@pytest.mark.asyncio
async def test_gated_ego_not_alerted(db, monkeypatch):
    """A long-silent ego that is GATED on a pending approval (gate ON) is a
    legitimate wait, not a stall — no alert."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=None, gate_on=True)
    await _seed(db, "user_ego_cycle", success_min_ago=3 * 24 * 60, intent_min_ago=5)
    await _seed_pending_approval(db, "user_ego_cycle")

    await loop._check_ego_liveness(db)

    assert await _open_count(db, USER_SRC) == 0


async def _seed_last_gated(db, source, *, minutes_ago):
    t = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    await db.execute(
        "INSERT INTO ego_state (key, value, updated_at) VALUES (?, ?, ?)",
        (f"last_gated:{source}", t, t),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_recent_gate_release_no_alert(db, monkeypatch):
    """The gate-RELEASE race: a pending CLI approval was just granted, so the
    ego is no longer `gated`, but `last_success` still trails until the
    unblocked cycle completes. Recently gate-held (last_gated 1m ago) → within
    the grace → no alert. WITHOUT the grace this stalled-shaped ego would
    falsely alert."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=None, gate_on=True)
    await _seed(db, "user_ego_cycle", success_min_ago=6 * 60 + 30, intent_min_ago=5)
    await _seed_last_gated(db, "user_ego_cycle", minutes_ago=1)

    await loop._check_ego_liveness(db)

    assert await _open_count(db, USER_SRC) == 0


@pytest.mark.asyncio
async def test_gate_release_grace_expired_still_alerts(db, monkeypatch):
    """Grace does not mask indefinitely: long after the gate released (well past
    one interval) a still-stale completion alerts as a genuine stall."""
    _wire(monkeypatch, user_mgr=_mgr(interval=90), genesis_mgr=None, gate_on=False)
    await _seed(db, "user_ego_cycle", success_min_ago=6 * 60 + 30, intent_min_ago=5)
    await _seed_last_gated(db, "user_ego_cycle", minutes_ago=300)  # 5h > 90m grace

    await loop._check_ego_liveness(db)

    assert await _open_count(db, USER_SRC) == 1


@pytest.mark.asyncio
async def test_idempotent_then_resolves_on_recovery(db, monkeypatch):
    """Repeated ticks while stalled keep exactly one open alert; once a cycle
    completes (last_success advances past the intent) the alert auto-resolves."""
    _wire(monkeypatch, user_mgr=_mgr(), genesis_mgr=None, gate_on=False)
    await _seed(db, "user_ego_cycle", success_min_ago=3 * 24 * 60, intent_min_ago=5)

    await loop._check_ego_liveness(db)
    await loop._check_ego_liveness(db)  # second tick must not duplicate
    assert await _open_count(db, USER_SRC) == 1

    # Ego recovers: last_success advances to now (past the intent).
    await db.execute(
        "UPDATE job_health SET last_success=? WHERE job_name='user_ego_cycle'",
        (NOW.isoformat(),),
    )
    await db.commit()
    await loop._check_ego_liveness(db)
    assert await _open_count(db, USER_SRC) == 0
