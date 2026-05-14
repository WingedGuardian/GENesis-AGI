"""Tests for genesis.sentinel.classifier — fire alarm tier classification."""

from __future__ import annotations

from genesis.sentinel.classifier import FireAlarm, classify_alerts, worst_tier


class TestClassifyAlerts:
    def test_empty_alerts(self):
        assert classify_alerts([]) == []

    def test_tier1_defense_failure(self):
        alerts = [
            {"id": "service:watchdog_blind", "severity": "WARNING", "message": "Watchdog inactive"},
        ]
        alarms = classify_alerts(alerts)
        assert len(alarms) == 1
        assert alarms[0].tier == 1
        assert alarms[0].is_defense_failure

    def test_tier1_guardian_heartbeat_stale(self):
        """Guardian heartbeat stale is a Tier 1 defense failure — the
        host-side safety net has lost visibility on the container.
        """
        alerts = [
            {
                "id": "guardian:heartbeat_stale",
                "severity": "CRITICAL",
                "message": "Guardian heartbeat not updating (stale 600s)",
            },
        ]
        alarms = classify_alerts(alerts)
        assert len(alarms) == 1
        assert alarms[0].tier == 1
        assert alarms[0].is_defense_failure

    def test_tier2_critical_infrastructure(self):
        alerts = [
            {"id": "memory:critical", "severity": "CRITICAL", "message": "Memory >90%"},
        ]
        alarms = classify_alerts(alerts)
        assert len(alarms) == 1
        assert alarms[0].tier == 2

    def test_tier2_generic_critical(self):
        """Any CRITICAL severity gets promoted to Tier 2 by the blanket rule.

        Severity is the contract: emitters that mark something CRITICAL mean
        it. The classifier trusts them and wakes Sentinel. Fixing dishonest
        emitters happens at the source (gate at health_alerts), not by
        silencing the listener.
        """
        alerts = [
            {"id": "unknown:thing", "severity": "CRITICAL", "message": "Something critical"},
        ]
        alarms = classify_alerts(alerts)
        assert len(alarms) == 1
        assert alarms[0].tier == 2  # Any CRITICAL is at least Tier 2

    def test_all_emitted_critical_ids_classify(self):
        """Regression: every CRITICAL alert id emitted by _impl_health_alerts
        MUST classify to Tier 1 or Tier 2.

        This is the trip-wire for emission/classification drift. If a new
        CRITICAL alert is added to mcp/health/errors.py without updating
        _TIER1_PATTERNS or _TIER2_PATTERNS (or being explicitly suppressed
        at the source), this test fails immediately.

        Source IDs verified against src/genesis/mcp/health/errors.py
        on 2026-04-09. When adding/removing CRITICAL alert sources, update
        both files together and add the new id to this list.
        """
        emitted_critical_ids = [
            "service:genesis_down",                # Tier 1
            # cc:quota_exhausted removed from this list in Part 9 — it
            # is now emitted at WARNING severity (mcp/health/errors.py)
            # and deliberately not in _TIER1_PATTERNS (see classifier.py
            # rationale). It routes to Tier 3 via the WARNING fall-through.
            "guardian:heartbeat_stale",             # Tier 1 (Part 8)
            "awareness:tick_overdue",               # Tier 1 (explicit — defense mechanism)
            "infra:disk_low",                       # Tier 2 (via blanket)
            "infra:container_memory_high",          # Tier 2 (via blanket)
            "infra:qdrant_collections_missing",     # Tier 2 (via blanket)
            "provider:embedding_failing",           # Tier 2 (via blanket)
            "provider:qdrant_unreachable",          # Tier 2 (via blanket)
            "genesis:update_failed",                # Tier 2 (via blanket)
        ]
        for alert_id in emitted_critical_ids:
            alarms = classify_alerts([
                {"id": alert_id, "severity": "CRITICAL", "message": f"test {alert_id}"},
            ])
            assert len(alarms) == 1, (
                f"{alert_id} did not classify — would be silently dropped. "
                f"Add to _TIER1_PATTERNS or _TIER2_PATTERNS, OR document why "
                f"it should be suppressed at the emitter in errors.py."
            )
            assert alarms[0].tier in (1, 2), (
                f"{alert_id} classified as Tier {alarms[0].tier}, expected 1 or 2"
            )

    def test_cc_quota_exhausted_not_tier1(self):
        """Part 9c: cc:quota_exhausted must NOT be in Tier 1.

        The Sentinel's only response tool is dispatching a CC session.
        If CC is unavailable, the diagnostic CC session cannot run —
        waking Sentinel for CC failures is self-defeating. The alert
        is downgraded to WARNING at the emitter and routes to Tier 3.
        """
        from genesis.sentinel.classifier import _TIER1_PATTERNS

        assert "cc:quota_exhausted" not in _TIER1_PATTERNS
        assert "cc:unavailable" not in _TIER1_PATTERNS

    def test_cc_quota_exhausted_warning_is_tier3(self):
        """With emission severity downgraded to WARNING, cc:quota_exhausted
        routes to Tier 3 via the default WARNING fall-through.
        """
        alerts = [
            {
                "id": "cc:quota_exhausted",
                "severity": "WARNING",
                "message": "CC rate limited — contingency mode active",
            },
        ]
        alarms = classify_alerts(alerts)
        assert len(alarms) == 1
        assert alarms[0].tier == 3

    def test_tier3_warning(self):
        alerts = [
            {"id": "some:warning", "severity": "WARNING", "message": "Something warned"},
        ]
        alarms = classify_alerts(alerts)
        assert len(alarms) == 1
        assert alarms[0].tier == 3

    def test_mixed_tiers_sorted(self):
        alerts = [
            {"id": "some:warning", "severity": "WARNING", "message": "Minor"},
            {"id": "service:watchdog_blind", "severity": "WARNING", "message": "Watchdog"},
            {"id": "memory:critical", "severity": "CRITICAL", "message": "Memory"},
        ]
        alarms = classify_alerts(alerts)
        assert len(alarms) == 3
        # Sorted by tier (worst first)
        assert alarms[0].tier == 1
        assert alarms[1].tier == 2
        assert alarms[2].tier == 3

    def test_call_site_down_is_tier3(self):
        """Call site DOWN alerts emit WARNING (not CRITICAL) because the
        Sentinel has no remediation path for provider circuit breakers.
        WARNING routes to Tier 3 (reflexes only, Sentinel stays asleep).
        """
        alerts = [
            {
                "id": "call_site:33_skill_refiner",
                "severity": "WARNING",
                "message": "Call site 33_skill_refiner is DOWN (all providers exhausted)",
            },
        ]
        alarms = classify_alerts(alerts)
        assert len(alarms) == 1
        assert alarms[0].tier == 3

    def test_info_severity_ignored(self):
        alerts = [
            {"id": "resolved:thing", "severity": "INFO", "message": "All good"},
        ]
        assert classify_alerts(alerts) == []


class TestWorstTier:
    def test_empty_returns_none(self):
        assert worst_tier([]) is None

    def test_single_alarm(self):
        assert worst_tier([FireAlarm(tier=2, alert_id="x", severity="CRITICAL", message="y")]) == 2

    def test_multiple_returns_lowest(self):
        alarms = [
            FireAlarm(tier=3, alert_id="a", severity="WARNING", message="minor"),
            FireAlarm(tier=1, alert_id="b", severity="WARNING", message="defense"),
        ]
        assert worst_tier(alarms) == 1
