"""Tests for the follow-up hygiene watchdog (_check_follow_up_watchdog).

Under test is the ALERTING state machine, not the CRUD queries: it fires ONE
deduped, self-resolving infrastructure_alert when hot-lane follow_ups are stuck
invisible (orphaned-scheduled: status='scheduled' with no linked_task_id) or
undispatched (past-due scheduled_task), and resolves on recovery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.awareness import loop
from genesis.db.crud import follow_ups as fu_crud
from genesis.db.schema import create_all_tables

pytestmark = pytest.mark.asyncio

SOURCE = "follow_up_watchdog"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def _reset_cooldowns(monkeypatch):
    monkeypatch.setattr(loop, "_last_fu_watchdog_alert_at", 0.0)
    monkeypatch.setattr(loop, "_last_fu_watchdog_alert_key", "")


async def _alerts(db) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM observations WHERE source = ? AND type = 'infrastructure_alert' "
        "AND resolved = 0",
        (SOURCE,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def _mk_orphaned_scheduled(db, *, age_hours: float = 48, priority: str = "high") -> str:
    """A row in status='scheduled' with NO linked_task_id (the black hole)."""
    res = await fu_crud.create(
        db,
        content="stuck scheduled item",
        reason="r",
        source="test",
        strategy="user_input_needed",
        kind="follow_up",
        priority=priority,
    )
    fid = res["id"] if isinstance(res, dict) else res
    created = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    await db.execute(
        "UPDATE follow_ups SET status='scheduled', linked_task_id=NULL, created_at=? WHERE id=?",
        (created, fid),
    )
    await db.commit()
    return fid


async def _mk_past_due(db, *, due_hours_ago: float = 48) -> str:
    res = await fu_crud.create(
        db,
        content="overdue scheduled task",
        reason="r",
        source="test",
        strategy="scheduled_task",
        kind="follow_up",
        scheduled_at=(datetime.now(UTC) - timedelta(hours=due_hours_ago)).isoformat(),
    )
    fid = res["id"] if isinstance(res, dict) else res
    await db.execute("UPDATE follow_ups SET status='pending' WHERE id=?", (fid,))
    await db.commit()
    return fid


# ── the two finding classes fire ─────────────────────────────────────────────


async def test_orphaned_scheduled_row_raises_alert(db):
    await _mk_orphaned_scheduled(db)
    await loop._check_follow_up_watchdog(db)
    alerts = await _alerts(db)
    assert len(alerts) == 1
    assert "orphaned-scheduled" in alerts[0]["content"]


async def test_past_due_scheduled_row_raises_alert(db):
    await _mk_past_due(db)
    await loop._check_follow_up_watchdog(db)
    alerts = await _alerts(db)
    assert len(alerts) == 1
    assert "past-due" in alerts[0]["content"]


async def test_dispatched_linked_scheduled_task_not_past_due(db):
    """A healthy scheduled_task that WAS dispatched is status='scheduled'+linked with
    a frozen past scheduled_at — it must NOT be reported as 'dispatcher never
    actuated' (the BLOCKER the review caught). It's tracked via get_linked_active."""
    res = await fu_crud.create(
        db,
        content="dispatched, waiting on idle surplus",
        reason="r",
        source="test",
        strategy="scheduled_task",
        kind="follow_up",
        scheduled_at=(datetime.now(UTC) - timedelta(hours=48)).isoformat(),
    )
    fid = res["id"] if isinstance(res, dict) else res
    await fu_crud.link_task(db, fid, "task-xyz")  # -> status='scheduled', linked_task_id set
    past_due = await fu_crud.get_past_due_scheduled(db, grace_hours=6)
    assert all(r["id"] != fid for r in past_due)
    await loop._check_follow_up_watchdog(db)
    assert await _alerts(db) == []


async def test_orphaned_scheduled_task_not_double_counted(db):
    """A genuine orphan that also happens to be a scheduled_task (status='scheduled',
    no link, old scheduled_at) is caught by get_orphaned_scheduled ONLY — never also
    by get_past_due_scheduled (which is now status='pending'-only), so it's listed
    once, not twice."""
    res = await fu_crud.create(
        db,
        content="orphaned scheduled_task",
        reason="r",
        source="test",
        strategy="scheduled_task",
        kind="follow_up",
        scheduled_at=(datetime.now(UTC) - timedelta(hours=48)).isoformat(),
    )
    fid = res["id"] if isinstance(res, dict) else res
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    await db.execute(
        "UPDATE follow_ups SET status='scheduled', linked_task_id=NULL, created_at=? WHERE id=?",
        (old, fid),
    )
    await db.commit()
    orphaned = await fu_crud.get_orphaned_scheduled(db)
    past_due = await fu_crud.get_past_due_scheduled(db, grace_hours=6)
    assert any(r["id"] == fid for r in orphaned)
    assert all(r["id"] != fid for r in past_due)  # not double-counted


# ── grace + exemptions ───────────────────────────────────────────────────────


async def test_row_within_grace_not_flagged(db):
    # created 1h ago, grace default 6h → not yet flagged.
    await _mk_orphaned_scheduled(db, age_hours=1)
    await loop._check_follow_up_watchdog(db)
    assert await _alerts(db) == []


async def test_linked_scheduled_row_not_flagged(db):
    """status='scheduled' WITH a linked_task_id is visible via get_linked_active —
    not an orphan, must not alert."""
    res = await fu_crud.create(
        db, content="linked", reason="r", source="test", strategy="surplus_task", kind="follow_up"
    )
    fid = res["id"] if isinstance(res, dict) else res
    await fu_crud.link_task(db, fid, "task-abc")  # sets linked_task_id + scheduled
    await loop._check_follow_up_watchdog(db)
    assert await _alerts(db) == []


async def test_tabled_lane_ignored(db):
    """A tabled (cold-lane) orphan is consciously off-surface — not flagged."""
    res = await fu_crud.create(
        db, content="cold", reason="r", source="test", strategy="ego_judgment", kind="tabled"
    )
    fid = res["id"] if isinstance(res, dict) else res
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    await db.execute(
        "UPDATE follow_ups SET status='scheduled', linked_task_id=NULL, created_at=? WHERE id=?",
        (old, fid),
    )
    await db.commit()
    await loop._check_follow_up_watchdog(db)
    assert await _alerts(db) == []


async def test_pinned_row_still_flagged(db):
    fid = await _mk_orphaned_scheduled(db)
    await db.execute("UPDATE follow_ups SET pinned=1 WHERE id=?", (fid,))
    await db.commit()
    await loop._check_follow_up_watchdog(db)
    assert len(await _alerts(db)) == 1


# ── dedup / supersede / resolve ──────────────────────────────────────────────


async def test_same_state_dedup_no_second_observation(db):
    await _mk_orphaned_scheduled(db)
    await loop._check_follow_up_watchdog(db)
    # Reset the in-process cooldown so only DB-level dedup can stop a duplicate.
    loop._last_fu_watchdog_alert_at = 0.0
    loop._last_fu_watchdog_alert_key = ""
    await loop._check_follow_up_watchdog(db)
    assert len(await _alerts(db)) == 1


async def test_resolves_when_rows_fixed(db):
    fid = await _mk_orphaned_scheduled(db)
    await loop._check_follow_up_watchdog(db)
    assert len(await _alerts(db)) == 1
    # Fix the row (give it a real terminal state).
    await fu_crud.update_status(db, fid, "completed")
    await loop._check_follow_up_watchdog(db)
    assert await _alerts(db) == []


async def test_offender_set_change_supersedes_stale_alert(db):
    """P1: when the offender SET changes but class+priority don't, the stale alert
    must be superseded and a NEW alert must name the currently-stuck row. The dedup
    key includes the offender ids, not just class+priority — otherwise the morning
    report keeps listing an already-fixed row and hides the newly-stuck one."""
    a = await _mk_orphaned_scheduled(db)
    await loop._check_follow_up_watchdog(db)
    alerts = await _alerts(db)
    assert len(alerts) == 1
    assert a[:8] in alerts[0]["content"]

    # Fix A, and strand a DIFFERENT row B of the SAME class + priority.
    await fu_crud.update_status(db, a, "completed")
    b = await _mk_orphaned_scheduled(db)
    # Reset the in-process cooldown so only DB-level dedup is under test.
    loop._last_fu_watchdog_alert_at = 0.0
    loop._last_fu_watchdog_alert_key = ""
    await loop._check_follow_up_watchdog(db)

    alerts = await _alerts(db)
    assert len(alerts) == 1
    assert b[:8] in alerts[0]["content"]  # the newly-stuck row surfaces
    assert a[:8] not in alerts[0]["content"]  # the fixed row no longer listed


async def test_remediation_prescribes_visible_status_change(db):
    """P2: the alert's remediation must prescribe a VISIBLE status change. A
    work_state change alone sets kind/revisit_condition, NOT status, so it can't
    un-strand an orphan — the advice must not tell the operator otherwise."""
    await _mk_orphaned_scheduled(db)
    await loop._check_follow_up_watchdog(db)
    content = (await _alerts(db))[0]["content"]
    assert "status='blocked'" in content
    # must NOT prescribe a bare work_state change as the repair
    assert "work_state='blocked_on_trigger'" not in content


async def test_critical_row_escalates_priority(db):
    await _mk_orphaned_scheduled(db, priority="critical")
    await loop._check_follow_up_watchdog(db)
    alerts = await _alerts(db)
    assert alerts[0]["priority"] == "critical"


# ── config gating ────────────────────────────────────────────────────────────


async def test_disabled_config_no_alert(db, monkeypatch):
    from genesis.awareness import follow_up_watchdog_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "is_enabled", lambda: False)
    await _mk_orphaned_scheduled(db)
    await loop._check_follow_up_watchdog(db)
    assert await _alerts(db) == []


async def test_env_kill_switch_no_alert(db, monkeypatch):
    monkeypatch.setenv("GENESIS_FOLLOW_UP_WATCHDOG_DISABLED", "1")
    await _mk_orphaned_scheduled(db)
    await loop._check_follow_up_watchdog(db)
    assert await _alerts(db) == []


async def test_disable_while_alert_open_resolves_it(db, monkeypatch):
    """Disabling the watchdog while an alert is open must RESOLVE it, not strand it
    unresolved forever (SHOULD-FIX from review)."""
    await _mk_orphaned_scheduled(db)
    await loop._check_follow_up_watchdog(db)
    assert len(await _alerts(db)) == 1
    from genesis.awareness import follow_up_watchdog_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "is_enabled", lambda: False)
    await loop._check_follow_up_watchdog(db)
    assert await _alerts(db) == []  # cleared, not stranded


async def test_check_never_raises_on_db_error(db):
    # A closed connection must degrade to a debug log, never raise into the tick.
    await db.close()
    await loop._check_follow_up_watchdog(db)  # must not raise


# ── config module degradation ────────────────────────────────────────────────


async def test_config_defaults_when_missing(monkeypatch, tmp_path):
    from genesis.awareness import follow_up_watchdog_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_base_path", lambda: tmp_path / "nope.yaml")
    cfg = cfg_mod.load_config()
    assert cfg["enabled"] is True
    assert cfg_mod.knob_int(cfg, "grace_hours") == 6


async def test_knob_int_clamps_bad_values():
    from genesis.awareness import follow_up_watchdog_config as cfg_mod

    assert cfg_mod.knob_int({"grace_hours": 0}, "grace_hours") == 6
    assert cfg_mod.knob_int({"grace_hours": -3}, "grace_hours") == 6
    assert cfg_mod.knob_int({"grace_hours": True}, "grace_hours") == 6
    assert cfg_mod.knob_int({"grace_hours": 12}, "grace_hours") == 12


async def test_alert_priority_falls_back_on_bad_value():
    from genesis.awareness import follow_up_watchdog_config as cfg_mod

    assert cfg_mod.alert_priority({"alert_priority": "bogus"}) == "high"
    assert cfg_mod.alert_priority({"alert_priority": "critical"}) == "critical"


# ── settings domain + validator ──────────────────────────────────────────────


async def test_settings_domain_registered():
    from genesis.mcp.health import settings

    assert "follow_up_watchdog" in settings._DOMAIN_REGISTRY
    assert "follow_up_watchdog" in settings._DOMAIN_VALIDATORS


async def test_validator_accepts_valid_and_rejects_bad():
    from genesis.mcp.health.settings import _validate_follow_up_watchdog as v

    assert v({"enabled": True, "grace_hours": 6, "max_listed": 5, "alert_priority": "high"}) == []
    assert v({"enabled": "yes"})  # non-bool
    assert v({"grace_hours": 0})  # non-positive int
    assert v({"grace_hours": -1})
    assert v({"alert_priority": "urgent"})  # bad enum
    assert v({"unknown_key": 1})  # unknown key
