"""Tests for the deterministic governance gate."""

from datetime import UTC, datetime

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
        quiet_hours=QuietHours(start="22:00", end="07:00", timezone="UTC"),
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=5,
        surplus_daily=1,
        morning_report_time="07:00",
        morning_report_timezone="UTC",
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
    )


# Narrow quiet window that won't interfere with test execution.
_NO_QUIET_HOURS = QuietHours(start="02:00", end="02:30", timezone="UTC")


def _cfg_no_quiet(**overrides):
    defaults = dict(
        quiet_hours=_NO_QUIET_HOURS,
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=5,
        surplus_daily=1,
        morning_report_time="07:00",
        morning_report_timezone="UTC",
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
