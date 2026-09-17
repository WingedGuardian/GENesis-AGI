"""Tests for the deterministic governance gate."""

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import outreach as outreach_crud
from genesis.db.schema import create_all_tables
from genesis.outreach.config import OutreachConfig, QuietHours
from genesis.outreach.governance import GovernanceGate, content_hash
from genesis.outreach.types import (
    GovernanceVerdict,
    OutreachCategory,
    OutreachRequest,
)


@pytest.fixture
def config():
    return OutreachConfig(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=5,
        surplus_daily=1,
        content_daily=3,
        notification_daily=10,
        morning_report_time="07:00",
        # morning_report_timezone removed — uses user_timezone()
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
    )


def _cfg_no_quiet(**overrides):
    # Quiet hours are pinned off for outreach tests by the autouse
    # _disable_quiet_hours fixture (conftest.py), so the governance quiet-hours
    # check can't flake on wall-clock time. The window value here is irrelevant.
    defaults = dict(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=5,
        surplus_daily=1,
        content_daily=3,
        notification_daily=10,
        morning_report_time="07:00",
        # morning_report_timezone removed — uses user_timezone()
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
    )
    defaults.update(overrides)
    return OutreachConfig(**defaults)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_blocker_bypasses_governance(config, db):
    gate = GovernanceGate(config, db)
    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="API down",
        context="All providers failed",
        salience_score=1.0,
        signal_type="critical_failure",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.BYPASS


@pytest.mark.asyncio
async def test_alert_bypasses_governance(config, db):
    gate = GovernanceGate(config, db)
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Circuit breaker open",
        context="Groq CB tripped",
        salience_score=0.6,
        signal_type="circuit_breaker",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.BYPASS


@pytest.mark.asyncio
async def test_surplus_below_threshold_denied(config, db):
    gate = GovernanceGate(config, db)
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Interesting finding",
        context="Found something",
        salience_score=0.5,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert "salience" in result.reason


@pytest.mark.asyncio
async def test_surplus_above_threshold_allowed(db):
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Valuable insight",
        context="Important finding",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.ALLOW


@pytest.mark.asyncio
async def test_quiet_hours_denies_non_bypass(db, monkeypatch):
    """Quiet hours deny a non-bypass category. Verified deterministically by
    forcing the quiet-hours check on — it reads the wall clock in production,
    so the test must not depend on the actual time it runs."""
    monkeypatch.setattr(
        "genesis.outreach.governance.GovernanceGate._in_quiet_hours",
        lambda self: True,
    )
    gate = GovernanceGate(_cfg_no_quiet(), db)
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Valuable insight",
        context="Important finding",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert any("quiet" in c for c in result.checks_failed)


@pytest.mark.asyncio
async def test_duplicate_denied(config, db):
    gate = GovernanceGate(config, db)
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db,
        id="existing-1",
        signal_type="surplus_insight",
        topic="Same topic",
        category="surplus",
        salience_score=0.9,
        channel="telegram",
        message_content="Earlier message",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "existing-1", delivered_at=now)

    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Same topic",
        context="Different context",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert "dedup" in result.reason


@pytest.mark.asyncio
async def test_task_lifecycle_notifications_never_deduped(config, db):
    """Task lifecycle notifications (task_progress/complete/alert) have a
    dedup window of 0 — they must never be suppressed. A 'Task completed' ping
    must still deliver even though a sibling task_complete ping for the SAME
    topic+category was just delivered (which would otherwise collide on the
    (signal_type, topic, category) dedup key)."""
    gate = GovernanceGate(config, db)
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db,
        id="prior-task-1",
        signal_type="task_complete",
        topic="Task abcd1234",
        category="alert",
        salience_score=0.5,
        channel="telegram",
        message_content="Deliverable ready and Gate-2 verified: ...",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "prior-task-1", delivered_at=now)

    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Task abcd1234",  # same topic as the prior ping
        context="Task completed: build a thing",
        salience_score=0.5,
        signal_type="task_complete",
        verbatim=True,
    )
    result = await gate.check(req)
    # ALERT bypasses governance once dedup passes; the key point is NOT DENY.
    assert result.verdict != GovernanceVerdict.DENY
    assert "dedup" not in result.checks_failed


@pytest.mark.asyncio
async def test_gate_disabled_alert_never_deduped(config, db):
    """Disabling the mandatory approval gate is a distinct security event each
    time — the ALERT must NEVER be dedup-swallowed. ALERT bypasses salience/
    quiet-hours but STILL runs _is_duplicate; without a 0-window for
    approval_gate_disabled it falls to the 24h default, so a second disable
    within a day is silently dropped and the owner never learns."""
    gate = GovernanceGate(config, db)
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db,
        id="prior-gate-1",
        signal_type="approval_gate_disabled",
        topic="Approval gate disabled",
        category="alert",
        salience_score=1.0,
        channel="telegram",
        message_content="manual_approval_required set to FALSE ...",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "prior-gate-1", delivered_at=now)

    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Approval gate disabled",  # same topic as the prior alert
        context="manual_approval_required was set to FALSE via the dashboard",
        salience_score=1.0,
        signal_type="approval_gate_disabled",
        verbatim=True,
    )
    result = await gate.check(req)
    assert result.verdict != GovernanceVerdict.DENY
    assert "dedup" not in result.checks_failed


@pytest.mark.asyncio
async def test_critical_observation_never_deduped(config, db):
    """The critical-observations pager (scheduler, submit_raw, fixed topic
    'Critical Observations') must NOT be dedup-suppressed — its own state machine
    (get_unsurfaced + mark_surfaced + in-memory guard) owns de-dup. Without a
    0-window, a fresh critical (incl. a gate-disable paged via the MCP path)
    fell to the 24h default and was dropped if any earlier critical batch shared
    the topic. Checked via is_duplicate (the submit_raw path)."""
    gate = GovernanceGate(config, db)
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db,
        id="prior-crit-1",
        signal_type="critical_observation",
        topic="Critical Observations",
        category="blocker",
        salience_score=1.0,
        channel="telegram",
        message_content="earlier unrelated critical batch",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "prior-crit-1", delivered_at=now)

    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="Critical Observations",  # same fixed topic as the prior batch
        context="Approval gate DISABLED via MCP settings tool (confirmed)",
        salience_score=1.0,
        signal_type="critical_observation",
    )
    assert await gate.is_duplicate(req) is False


@pytest.mark.asyncio
async def test_rate_limit_exceeded(config, db):
    gate = GovernanceGate(config, db)
    now = datetime.now(UTC).isoformat()
    for i in range(5):
        await outreach_crud.create(
            db,
            id=f"fill-{i}",
            signal_type="surplus_insight",
            topic=f"Topic {i}",
            category="surplus",
            salience_score=0.9,
            channel="telegram",
            message_content=f"Message {i}",
            created_at=now,
        )
        await outreach_crud.record_delivery(db, f"fill-{i}", delivered_at=now)

    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="One more",
        context="Should be denied",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert "rate_limit" in result.reason


@pytest.mark.asyncio
async def test_surplus_quota_enforced(config, db):
    gate = GovernanceGate(config, db)
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db,
        id="surplus-today",
        signal_type="surplus_insight",
        topic="Already sent",
        category="surplus",
        salience_score=0.9,
        channel="telegram",
        message_content="Today's surplus",
        created_at=now,
        labeled_surplus=1,
    )
    await outreach_crud.record_delivery(db, "surplus-today", delivered_at=now)

    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Second surplus",
        context="Should be denied",
        salience_score=0.9,
        signal_type="surplus_insight",
        labeled_surplus=True,
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert "surplus_quota" in result.reason


# ---------------------------------------------------------------------------
# Enhanced dedup tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_signal_types_not_deduped(db):
    """Same topic+category but different signal_type should NOT be deduped."""
    cfg = _cfg_no_quiet(surplus_daily=10)
    gate = GovernanceGate(cfg, db)
    now = datetime.now(UTC).isoformat()

    await outreach_crud.create(
        db,
        id="health-1",
        signal_type="health_alert",
        topic="CPU high",
        category="surplus",
        salience_score=0.9,
        channel="telegram",
        message_content="CPU is high",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "health-1", delivered_at=now)

    # Same topic+category but different signal_type — should be allowed
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="CPU high",
        context="CPU is high again from different source",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.ALLOW


@pytest.mark.asyncio
async def test_expired_window_allows_resend(db):
    """Records outside the dedup window should not block new sends."""
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)

    # Insert a record with delivered_at 25 hours ago (outside 24h window)
    await db.execute(
        """INSERT INTO outreach_history
           (id, signal_type, topic, category, salience_score, channel,
            message_content, delivered_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '-25 hours'), datetime('now', '-25 hours'))""",
        ("old-1", "surplus_insight", "Old topic", "surplus", 0.9, "telegram", "Old msg"),
    )
    await db.commit()

    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Old topic",
        context="Same old topic, but window expired",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.ALLOW


@pytest.mark.asyncio
async def test_iso_t_delivered_at_outside_window_not_deduped(db):
    """Regression: ISO-8601 'T'-separator delivered_at must compare correctly
    against SQLite's space-separated datetime('now', ?).

    Production stores delivered_at via ``datetime.now(UTC).isoformat()`` →
    ``'2026-06-19T03:18:54+00:00'`` (literal 'T'). A *raw* string comparison
    treats 'T' (0x54) > ' ' (0x20), so a row delivered earlier on the cutoff's
    own UTC date sorts as "newer" than the space-separated cutoff — the dedup
    window then never expires within a calendar day. Wrapping the column in
    ``datetime(delivered_at)`` makes SQLite parse both sides before comparing.

    The existing expired-window test uses ``datetime('now', '-25 hours')`` for
    delivered_at — that value is space-separated, so it never exercises this bug.
    """
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)

    # surplus_insight uses the 24h default window. Anchor delivered_at to the
    # START of the cutoff's UTC date, in ISO-'T' format: strictly earlier than
    # the cutoff in real time (=> OUTSIDE the window) yet sharing the cutoff's
    # date prefix (=> the exact T-vs-space trigger). The guard covers the rare
    # (~1s/day) case where the cutoff itself lands at midnight.
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    delivered_dt = cutoff.replace(hour=0, minute=0, second=1, microsecond=0)
    if delivered_dt >= cutoff:
        delivered_dt -= timedelta(days=1)
    delivered = delivered_dt.isoformat()

    await outreach_crud.create(
        db,
        id="iso-old",
        signal_type="surplus_insight",
        topic="Old ISO topic",
        category="surplus",
        salience_score=0.9,
        channel="telegram",
        message_content="Old",
        created_at=delivered,
    )
    await outreach_crud.record_delivery(db, "iso-old", delivered_at=delivered)

    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Old ISO topic",
        context="Same topic, window long expired",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    # delivered_at is > 24h old → must ALLOW. The pre-fix raw-string compare
    # wrongly DENYs because 'T' (0x54) > ' ' (0x20).
    assert result.verdict == GovernanceVerdict.ALLOW


@pytest.mark.asyncio
async def test_blocker_deduped_when_duplicate(db):
    """Blocker should be deduped when a recent identical message exists.

    BLOCKER/ALERT bypass salience, quiet hours, and engagement throttle,
    but NOT dedup — repeated identical alerts add noise, not information.
    """
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)
    now = datetime.now(UTC).isoformat()

    await outreach_crud.create(
        db,
        id="blocker-prev",
        signal_type="critical_failure",
        topic="DB down",
        category="blocker",
        salience_score=1.0,
        channel="telegram",
        message_content="Database is down",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "blocker-prev", delivered_at=now)

    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="DB down",
        context="Database is down",
        salience_score=1.0,
        signal_type="critical_failure",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert "dedup" in result.checks_failed


@pytest.mark.asyncio
async def test_blocker_bypasses_when_not_duplicate(db):
    """Blocker should bypass governance when no prior exists."""
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)
    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="DB down",
        context="Database is down",
        salience_score=1.0,
        signal_type="critical_failure",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.BYPASS


@pytest.mark.asyncio
async def test_alert_deduped_when_duplicate(db):
    """Alert should be deduped when a recent identical message exists.

    BLOCKER/ALERT bypass salience, quiet hours, and engagement throttle,
    but NOT dedup — repeated identical alerts add noise, not information.
    """
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)
    now = datetime.now(UTC).isoformat()

    await outreach_crud.create(
        db,
        id="alert-prev",
        signal_type="circuit_breaker",
        topic="CB open",
        category="alert",
        salience_score=0.8,
        channel="telegram",
        message_content="Circuit breaker tripped",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "alert-prev", delivered_at=now)

    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="CB open",
        context="Circuit breaker tripped",
        salience_score=0.8,
        signal_type="circuit_breaker",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert "dedup" in result.checks_failed


@pytest.mark.asyncio
async def test_alert_bypasses_when_not_duplicate(db):
    """Alert should bypass governance when no prior exists."""
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="CB open",
        context="Circuit breaker tripped",
        salience_score=0.8,
        signal_type="circuit_breaker",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.BYPASS


@pytest.mark.asyncio
async def test_content_hash_dedup(db):
    """Different topics but same context content should be deduped via content hash."""
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)
    now = datetime.now(UTC).isoformat()
    context_text = "The Groq circuit breaker has been open for 15 minutes affecting all requests"

    await outreach_crud.create(
        db,
        id="hash-1",
        signal_type="surplus_insight",
        topic="Groq CB open — 15min",
        category="surplus",
        salience_score=0.9,
        channel="telegram",
        message_content="Formatted: " + context_text,
        content_hash=content_hash(context_text),
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "hash-1", delivered_at=now)

    # Different topic string, same context content
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Groq circuit breaker still open",
        context=context_text,
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert "dedup" in result.reason


@pytest.mark.asyncio
async def test_content_hash_different_content_allowed(db):
    """Same signal_type+category but different content hash should pass dedup."""
    cfg = _cfg_no_quiet(surplus_daily=10)
    gate = GovernanceGate(cfg, db)
    now = datetime.now(UTC).isoformat()

    await outreach_crud.create(
        db,
        id="hash-2",
        signal_type="surplus_insight",
        topic="Memory usage high",
        category="surplus",
        salience_score=0.9,
        channel="telegram",
        message_content="Memory at 90%",
        content_hash=content_hash("Memory at 90%"),
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "hash-2", delivered_at=now)

    # Different topic AND different context — should be allowed
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="CPU usage high",
        context="CPU at 95% for 10 minutes",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.ALLOW


@pytest.mark.asyncio
async def test_health_alert_uses_12h_window(db):
    """health_alert signal_type should use 12h dedup window."""
    cfg = _cfg_no_quiet(surplus_daily=10)
    gate = GovernanceGate(cfg, db)

    # Insert record 13 hours ago — outside 12h window but inside 24h
    await db.execute(
        """INSERT INTO outreach_history
           (id, signal_type, topic, category, salience_score, channel,
            message_content, delivered_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '-13 hours'), datetime('now', '-13 hours'))""",
        ("ha-old", "health_alert", "Disk full", "surplus", 0.9, "telegram", "Disk usage critical"),
    )
    await db.commit()

    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Disk full",
        context="Disk usage critical",
        salience_score=0.9,
        signal_type="health_alert",
    )
    result = await gate.check(req)
    # Should ALLOW because 13h > 12h window for health_alert
    assert result.verdict == GovernanceVerdict.ALLOW


@pytest.mark.asyncio
async def test_health_alert_within_6h_blocked(db):
    """health_alert within 6h window should be deduped."""
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)

    # Insert record 3 hours ago — inside 6h window
    await db.execute(
        """INSERT INTO outreach_history
           (id, signal_type, topic, category, salience_score, channel,
            message_content, delivered_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '-3 hours'), datetime('now', '-3 hours'))""",
        ("ha-recent", "health_alert", "Disk full", "surplus", 0.9, "telegram", "Disk usage critical"),
    )
    await db.commit()

    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Disk full",
        context="Disk usage critical",
        salience_score=0.9,
        signal_type="health_alert",
    )
    result = await gate.check(req)
    assert result.verdict == GovernanceVerdict.DENY
    assert "dedup" in result.reason


# ---------------------------------------------------------------------------
# content_hash unit tests
# ---------------------------------------------------------------------------


def test_content_hash_deterministic():
    assert content_hash("hello world") == content_hash("hello world")


def test_content_hash_truncates_at_200():
    long_a = "a" * 300
    long_b = "a" * 200 + "b" * 100
    # Both have same first 200 chars
    assert content_hash(long_a) == content_hash(long_b)


def test_content_hash_different_inputs():
    assert content_hash("alpha") != content_hash("beta")


@pytest.mark.asyncio
async def test_dedup_window_zero_skips_check(db):
    """Signal types with window_hours=0 (e.g. cli_approval) must never
    be deduped, even when an identical message was just delivered."""
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)
    now = datetime.now(UTC).isoformat()

    # Deliver a cli_approval message
    await outreach_crud.create(
        db,
        id="approval-1",
        signal_type="cli_approval",
        topic="Approval: ego cycle",
        category="approval",
        salience_score=1.0,
        channel="telegram",
        message_content="Approve ego cycle?",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, "approval-1", delivered_at=now)

    # Identical signal_type + topic — window=0 means never dedup
    req = OutreachRequest(
        category=OutreachCategory.APPROVAL,
        topic="Approval: ego cycle",
        context="Approve ego cycle?",
        salience_score=1.0,
        signal_type="cli_approval",
    )
    is_dup = await gate.is_duplicate(req)
    assert is_dup is False


@pytest.mark.asyncio
async def test_provision_approval_never_deduped(db):
    """provision_approval / provision_outcome must never be deduped (window=0).

    A grow approval that timed out UNANSWERED leaves a delivered_at row; at the
    old 24h default that row REJECTed the retry for 24h (Phase-C, 2026-07-18).
    Like cli_approval (#143), every provision approval/outcome must be delivered.
    """
    cfg = _cfg_no_quiet()
    gate = GovernanceGate(cfg, db)
    now = datetime.now(UTC).isoformat()

    for i, signal in enumerate(("provision_approval", "provision_outcome")):
        oid = f"prov-{i}"
        await outreach_crud.create(
            db,
            id=oid,
            signal_type=signal,
            topic="Grow container root to 40 GiB?",
            category="blocker",
            salience_score=1.0,
            channel="telegram",
            message_content="reply APPROVE / DENY",
            created_at=now,
        )
        await outreach_crud.record_delivery(db, oid, delivered_at=now)

        # Identical signal_type + topic + category, delivered just now. window=0
        # means never dedup, so a legitimate retry is not suppressed.
        req = OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="Grow container root to 40 GiB?",
            context="reply APPROVE / DENY",
            salience_score=1.0,
            signal_type=signal,
        )
        assert await gate.is_duplicate(req) is False

        # And the full blocker gate BYPASSes (delivers), never DENY-suppressed.
        result = await gate.check(req)
        assert result.verdict == GovernanceVerdict.BYPASS
        assert "dedup" not in result.checks_failed


# ── Quiet hours: the zero-width DISABLED window (shipped default) ────────────
#
# CAPTURED AT IMPORT, BEFORE ANY FIXTURE RUNS. tests/test_outreach/conftest.py
# has an AUTOUSE fixture that replaces GovernanceGate._in_quiet_hours with
# `lambda self: False` to keep the rest of this suite off the wall clock. Every
# test below would be VACUOUS against that stub -- it returns False for any
# config, which is the very answer two of them assert. So these call the real
# function object directly, and the guard-the-guard below proves we captured the
# real one rather than the stub.
_REAL_IN_QUIET_HOURS = GovernanceGate.__dict__["_in_quiet_hours"]


class _QH:
    """Minimal stand-in for GovernanceGate: _in_quiet_hours reads only this."""

    def __init__(self, start: str, end: str) -> None:
        self._config = OutreachConfig(
            quiet_hours=QuietHours(start=start, end=end),
            channel_preferences={"default": "telegram"},
            thresholds={},
            max_daily=5,
            surplus_daily=1,
            content_daily=3,
            notification_daily=10,
            morning_report_time="07:00",
            engagement_timeout_hours=24,
            engagement_poll_minutes=60,
        )


def test_captured_the_real_method_not_the_autouse_stub():
    """Guard-the-guard: if this fails, every test below proves nothing."""
    import inspect

    src = inspect.getsource(_REAL_IN_QUIET_HOURS)
    assert "quiet_hours" in src and "lambda" not in src.split("\n")[0], (
        "captured the autouse fixture's stub instead of the real method — the "
        "zero-width tests would pass for the wrong reason"
    )


def test_zero_width_window_disables_quiet_hours():
    """start == end means OFF. This is the shipped default (owner ruling).

    Without the rule the equal case falls into the `start <= end` branch and
    compares a microsecond-precision `now` against midnight: effectively-never
    true, but by accident rather than by contract.
    """
    assert _REAL_IN_QUIET_HOURS(_QH("00:00", "00:00")) is False
    # Not special-cased to midnight — any equal pair is a zero-width window.
    assert _REAL_IN_QUIET_HOURS(_QH("13:37", "13:37")) is False


def test_a_window_containing_now_is_still_detected():
    """THE FALSIFIER for 'did the new rule disable quiet hours entirely'.

    Derived from the same clock the method reads, so it cannot flake: a window
    spanning [now-1h, now+1h] must always contain now, including across midnight
    (start > end then, which the wrap branch handles).
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from genesis.env import user_timezone

    try:
        tz = ZoneInfo(user_timezone())
    except Exception:
        tz = UTC
    now = datetime.now(tz)
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")

    assert _REAL_IN_QUIET_HOURS(_QH(start, end)) is True, (
        f"window {start}-{end} must contain now ({now:%H:%M}) — if this fails the "
        f"zero-width rule has short-circuited the whole check"
    )


def test_a_window_not_containing_now_is_not_quiet():
    """The other direction, same clock-derived construction."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from genesis.env import user_timezone

    try:
        tz = ZoneInfo(user_timezone())
    except Exception:
        tz = UTC
    now = datetime.now(tz)
    start = (now + timedelta(hours=2)).strftime("%H:%M")
    end = (now + timedelta(hours=3)).strftime("%H:%M")

    assert _REAL_IN_QUIET_HOURS(_QH(start, end)) is False


def test_every_default_path_ships_quiet_hours_disabled(tmp_path):
    """ONE test locking ALL THREE default paths.

    A default can arrive from three places, and setting only one leaves installs
    quieted on the other two:
      1. no config file at all            -> _DEFAULTS
      2. a file with no quiet_hours block -> the per-key .get() fallbacks
      3. the shipped config/outreach.yaml -> the file itself
    """
    from pathlib import Path

    from genesis.outreach.config import (
        _DEFAULTS,
        QUIET_HOURS_DISABLED,
        load_outreach_config,
    )

    # 1. No file.
    missing = load_outreach_config(tmp_path / "nope.yaml")
    assert missing.quiet_hours == QUIET_HOURS_DISABLED, "no-file default is quieted"
    assert _DEFAULTS.quiet_hours == QUIET_HOURS_DISABLED

    # 2. A file that exists but declares no quiet_hours.
    partial = tmp_path / "partial.yaml"
    partial.write_text("rate_limits:\n  max_daily: 5\n")
    assert load_outreach_config(partial).quiet_hours == QUIET_HOURS_DISABLED, (
        "a config saved before this key existed must not re-enable quiet hours"
    )

    # 3. The SHIPPED repo config. Read the FILE, not load_outreach_config(file):
    # the loader applies merge_local_overlay, so an install that legitimately
    # re-enables quiet hours in outreach.local.yaml would turn this repo test red
    # and blame the innocent shipped yaml. The claim here is about what the repo
    # SHIPS, so assert on the shipped bytes.
    import yaml

    shipped = Path(__file__).resolve().parents[2] / "config" / "outreach.yaml"
    assert shipped.exists(), shipped
    shipped_qh = (yaml.safe_load(shipped.read_text()) or {}).get("quiet_hours", {})
    assert shipped_qh.get("start") == QUIET_HOURS_DISABLED.start, shipped_qh
    assert shipped_qh.get("end") == QUIET_HOURS_DISABLED.end, shipped_qh
    assert shipped_qh["start"] == shipped_qh["end"], (
        "config/outreach.yaml must ship a zero-width (disabled) window"
    )

    # And the disabled value must actually read as disabled.
    assert _REAL_IN_QUIET_HOURS(
        _QH(QUIET_HOURS_DISABLED.start, QUIET_HOURS_DISABLED.end)
    ) is False


def test_midnight_is_the_instant_the_rule_actually_decides(monkeypatch):
    """The ONE case where the zero-width rule is load-bearing rather than cosmetic.

    Honest about my own change: DELETING `if start == end: return False` is almost
    a no-op, because a zero-width window then falls into the `start <= end` branch
    and evaluates `00:00 <= now <= 00:00`, which is False at every instant except
    exactly midnight. Measured: a mutation removing the early return left every
    other test in this file GREEN.

    So this test freezes the clock AT midnight, where the two behaviours diverge:
    with the rule, quiet hours are off; without it, a disabled window silently
    ENGAGES. That makes "zero-width means disabled" a contract the suite can
    defend, rather than an accident of how the comparison happens to evaluate.
    """
    import datetime as _dt

    class _MidnightDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - signature must match
            return _dt.datetime(2026, 9, 11, 0, 0, 0, 0, tzinfo=_dt.UTC)

    monkeypatch.setattr("genesis.outreach.governance.datetime", _MidnightDatetime)

    # Guard-the-guard: the patch must actually be in force, or this proves nothing.
    from genesis.outreach import governance as gov_mod

    assert gov_mod.datetime.now(UTC).hour == 0
    assert gov_mod.datetime.now(UTC).minute == 0
    assert gov_mod.datetime.now(UTC).microsecond == 0

    assert _REAL_IN_QUIET_HOURS(_QH("00:00", "00:00")) is False, (
        "at exactly midnight a zero-width window must still read as DISABLED — "
        "this is the instant where removing the start == end rule would silently "
        "turn quiet hours back on"
    )
    # Control: a REAL window containing midnight must still read as quiet, so the
    # rule above is narrow rather than a blanket 'never quiet'.
    assert _REAL_IN_QUIET_HOURS(_QH("22:00", "07:00")) is True


@pytest.mark.parametrize(
    "start,end",
    [
        ("", ""),            # empty — what a cleared dashboard field sends
        ("25:99", "07:00"),  # out of range
        ("10 PM", "7 AM"),   # human-written, not HH:MM
        (None, None),        # key present but null in yaml
    ],
)
def test_a_malformed_window_disables_rather_than_raising(start, end, caplog):
    """A typo must not brick outreach.

    _in_quiet_hours runs from check() on EVERY send, and scheduler.py retries a
    failed drain indefinitely — so an unparseable value turned one typo into a
    permanent 5-minute failure loop. MEASURED before the fix: `""` raised
    `ValueError: Invalid isoformat string: ''` and `"25:99"` raised
    `ValueError: hour must be in 0..23`. Nothing validates this field (the
    settings domain registers no validator and the dashboard PUTs the config
    through unchecked), and this change is what invites editing it.

    Fails toward DISABLED: the owner keeps getting their messages, and the bad
    value is logged rather than swallowed.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="genesis.outreach.governance"):
        assert _REAL_IN_QUIET_HOURS(_QH(start, end)) is False
    assert any("Invalid quiet_hours" in r.message for r in caplog.records), (
        "failing toward disabled must still be LOUD — a silently-ignored typo is "
        "how someone concludes quiet hours works when it does not"
    )
