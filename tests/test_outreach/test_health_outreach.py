"""Tests for HealthOutreachBridge — health alerts → outreach requests.

Only alerts in the immediate_escalation whitelist with CRITICAL severity
reach Telegram. Everything else is internal-only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.outreach.health_outreach import HealthOutreachBridge
from genesis.outreach.types import OutreachCategory

# Default escalation IDs matching production config
_ESCALATION_IDS = frozenset({
    "infra:tmpfs_low",
    "infra:disk_low",
    "infra:container_memory_high",
    "awareness:tick_overdue",
})


@pytest.fixture
def bridge(db):
    return HealthOutreachBridge(db, escalation_ids=_ESCALATION_IDS)


@pytest.fixture
def bridge_no_escalation(db):
    """Bridge with empty escalation set — nothing reaches Telegram."""
    return HealthOutreachBridge(db, escalation_ids=frozenset())


def _patch_alerts(alerts):
    return patch(
        "genesis.mcp.health_mcp._impl_health_alerts",
        new_callable=AsyncMock,
        return_value=alerts,
    )


# ── Basic filtering ──────────────────────────────────────────────────────


async def test_no_alerts_returns_empty(bridge):
    with _patch_alerts([]):
        result = await bridge.check_and_generate()
    assert result == []


async def test_escalation_alert_critical_passes_through(bridge):
    """CRITICAL alert in the escalation whitelist should generate a request."""
    alerts = [
        {
            "id": "infra:tmpfs_low",
            "severity": "CRITICAL",
            "message": "/tmp tmpfs at 8% free (41MB) — filling /tmp kills CC sessions",
        },
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert len(result) == 1
    assert result[0].category == OutreachCategory.BLOCKER
    assert result[0].salience_score == 1.0
    assert result[0].signal_type == "health_alert"
    assert result[0].source_id == "infra:tmpfs_low"
    assert "/tmp" in result[0].context


async def test_all_escalation_ids_pass_at_critical(bridge):
    """All whitelisted alert IDs should pass through when CRITICAL."""
    alerts = [
        {"id": "infra:tmpfs_low", "severity": "CRITICAL", "message": "/tmp low"},
        {"id": "infra:disk_low", "severity": "CRITICAL", "message": "Disk low"},
        {"id": "infra:container_memory_high", "severity": "CRITICAL", "message": "Memory at 92%"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert len(result) == 3
    assert all(r.category == OutreachCategory.BLOCKER for r in result)


# ── Non-escalation alerts are filtered out ───────────────────────────────


async def test_call_site_down_filtered(bridge):
    """Call-site-down alerts should NOT reach Telegram (router handles failover)."""
    alerts = [
        {"id": "call_site:7", "severity": "CRITICAL", "message": "Call site 7 DOWN"},
        {"id": "call_site:15", "severity": "CRITICAL", "message": "Call site 15 DOWN"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert result == []


async def test_warning_alerts_filtered(bridge):
    """WARNING alerts should NOT pass even if they're in the escalation list."""
    alerts = [
        {"id": "infra:tmpfs_low", "severity": "WARNING", "message": "/tmp at 18% free"},
        {"id": "infra:disk_low", "severity": "WARNING", "message": "Disk at 8% free"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert result == []


async def test_non_escalation_warning_filtered(bridge):
    """WARNING alerts not in escalation list should be filtered."""
    alerts = [
        {"id": "cc:budget", "severity": "WARNING", "message": "CC budget throttled"},
        {"id": "queue:stale_dead_letters", "severity": "WARNING", "message": "Dead letters stale"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert result == []


async def test_awareness_tick_overdue_escalates(bridge):
    """CRITICAL awareness:tick_overdue alert in escalation whitelist should reach Telegram."""
    alerts = [
        {"id": "awareness:tick_overdue", "severity": "CRITICAL", "message": "Awareness tick overdue by 500s (>360s threshold)"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert len(result) == 1
    assert result[0].category == OutreachCategory.BLOCKER
    assert result[0].source_id == "awareness:tick_overdue"


async def test_info_alerts_filtered(bridge):
    alerts = [
        {"id": "info:something", "severity": "INFO", "message": "All good"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert result == []


# ── Mixed alert scenarios ────────────────────────────────────────────────


async def test_mixed_alerts_only_critical_escalation_passes(bridge):
    """Only CRITICAL + escalation-whitelisted alerts pass through."""
    alerts = [
        {"id": "call_site:7", "severity": "CRITICAL", "message": "Call site 7 DOWN"},
        {"id": "infra:tmpfs_low", "severity": "CRITICAL", "message": "/tmp at 5%"},
        {"id": "infra:disk_low", "severity": "WARNING", "message": "Disk at 8%"},
        {"id": "cc:budget", "severity": "WARNING", "message": "Budget throttled"},
        {"id": "infra:container_memory_high", "severity": "CRITICAL", "message": "Memory at 93%"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert len(result) == 2
    ids = {r.source_id for r in result}
    assert ids == {"infra:tmpfs_low", "infra:container_memory_high"}


async def test_empty_escalation_ids_blocks_everything(bridge_no_escalation):
    """With no escalation IDs configured, nothing reaches Telegram."""
    alerts = [
        {"id": "infra:tmpfs_low", "severity": "CRITICAL", "message": "/tmp at 5%"},
        {"id": "service:bridge_down", "severity": "CRITICAL", "message": "Bridge dead"},
    ]
    with _patch_alerts(alerts):
        result = await bridge_no_escalation.check_and_generate()

    assert result == []


# ── Dedup ────────────────────────────────────────────────────────────────


async def test_dedup_suppresses_recently_sent(db, bridge):
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO outreach_history "
        "(id, signal_type, topic, category, salience_score, channel, "
        " message_content, created_at, delivered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "oh-1", "health_alert",
            "Infrastructure Alert: infra:tmpfs_low",
            "blocker", 1.0, "telegram", "/tmp low", now, now,
        ),
    )
    await db.commit()

    alerts = [
        {"id": "infra:tmpfs_low", "severity": "CRITICAL", "message": "/tmp at 5%"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert result == []  # Suppressed by dedup


async def test_dedup_does_not_suppress_undelivered(db, bridge):
    """Alert created but never delivered should NOT be suppressed."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO outreach_history "
        "(id, signal_type, topic, category, salience_score, channel, "
        " message_content, created_at, delivered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "oh-2", "health_alert",
            "Infrastructure Alert: infra:tmpfs_low",
            "blocker", 1.0, "telegram", "/tmp low", now, None,
        ),
    )
    await db.commit()

    alerts = [
        {"id": "infra:tmpfs_low", "severity": "CRITICAL", "message": "/tmp at 5%"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert len(result) == 1


async def test_dedup_does_not_suppress_old_delivery(db, bridge):
    """Alert delivered 7h ago (outside 6h window) should NOT be suppressed."""
    old = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    await db.execute(
        "INSERT INTO outreach_history "
        "(id, signal_type, topic, category, salience_score, channel, "
        " message_content, created_at, delivered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "oh-3", "health_alert",
            "Infrastructure Alert: infra:tmpfs_low",
            "blocker", 1.0, "telegram", "/tmp low", old, old,
        ),
    )
    await db.commit()

    alerts = [
        {"id": "infra:tmpfs_low", "severity": "CRITICAL", "message": "/tmp at 5%"},
    ]
    with _patch_alerts(alerts):
        result = await bridge.check_and_generate()

    assert len(result) == 1
