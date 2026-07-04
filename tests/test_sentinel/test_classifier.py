"""Tests for genesis.sentinel.classifier — scope gate + fire alarm tiers.

Classification is two-stage (see classifier docstring): scope (is the alert
remediable with an available tool?) then tier. These tests pin BOTH stages:
the boundary test enumerates every alert id the health emitter can produce
and requires an explicit disposition (mapped or unmapped-by-design), so a
new CRITICAL emitter can neither silently bypass the Sentinel nor silently
wake it.
"""

from __future__ import annotations

import re
from pathlib import Path

from genesis.sentinel.classifier import (
    _TIER1_PATTERNS,
    FireAlarm,
    classify_alerts,
    worst_tier,
)
from genesis.sentinel.remediation_map import (
    REMEDIATION_MAP,
    REMEDIATION_PREFIX_MAP,
    TOOLS,
    UNMAPPED_BY_DESIGN,
    available_tools,
    is_remediable,
    required_tools,
)

# Every tool available — makes remediability deterministic in tier tests so
# the only variable is the map, not this machine's install shape.
FULL_SCOPE = frozenset(t.id for t in TOOLS)


class TestClassifyAlerts:
    def test_empty_alerts(self):
        assert classify_alerts([], FULL_SCOPE) == []

    def test_tier1_defense_failure(self):
        alerts = [
            {"id": "service:watchdog_blind", "severity": "WARNING", "message": "Watchdog inactive"},
        ]
        alarms = classify_alerts(alerts, FULL_SCOPE)
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
        alarms = classify_alerts(alerts, FULL_SCOPE)
        assert len(alarms) == 1
        assert alarms[0].tier == 1
        assert alarms[0].is_defense_failure

    def test_tier2_critical_infrastructure(self):
        alerts = [
            {"id": "infra:container_memory_high", "severity": "CRITICAL", "message": "Memory >90%"},
        ]
        alarms = classify_alerts(alerts, FULL_SCOPE)
        assert len(alarms) == 1
        assert alarms[0].tier == 2

    def test_unmapped_critical_is_fail_closed(self):
        """An alert id with NO remediation mapping never classifies, even at
        CRITICAL. This inverts the old blanket rule ("any CRITICAL wakes the
        Sentinel"): the Sentinel is an emergency responder and is only woken
        for fires it can actually put out. Severity is still the contract
        for URGENCY of in-scope alerts; scope is the contract for WHETHER it
        is the Sentinel's fire at all. Fail-closed is safe because the
        boundary test below forces every emitted id to take an explicit
        disposition — nothing can be unmapped by accident.
        """
        alerts = [
            {"id": "unknown:thing", "severity": "CRITICAL", "message": "Something critical"},
        ]
        assert classify_alerts(alerts, FULL_SCOPE) == []

    def test_scope_relative_guardian(self):
        """The same alert classifies or not depending on available tools —
        an install without a Guardian configured has no SSH-restart path,
        so a stale-guardian alarm cannot wake a Sentinel that can't act.
        """
        alerts = [
            {"id": "guardian:heartbeat_stale", "severity": "CRITICAL", "message": "stale"},
        ]
        without = FULL_SCOPE - {"guardian.ssh_restart"}
        assert classify_alerts(alerts, without) == []
        assert len(classify_alerts(alerts, FULL_SCOPE)) == 1

    def test_cc_quota_exhausted_not_tier1(self):
        """cc:quota_exhausted must NOT be in Tier 1.

        The Sentinel's only response tool is dispatching a CC session.
        If CC is unavailable, the diagnostic CC session cannot run —
        waking Sentinel for CC failures is self-defeating.
        """
        assert "cc:quota_exhausted" not in _TIER1_PATTERNS
        assert "cc:unavailable" not in _TIER1_PATTERNS

    def test_cc_quota_exhausted_never_classifies(self):
        """cc:* alerts are unmapped-by-design (self-defeating loop), so they
        no longer classify at any severity — previously the WARNING routed
        to inert Tier 3; now the scope stage drops it entirely. The alert
        itself stays visible (dashboard + outreach whitelist).
        """
        for severity in ("WARNING", "CRITICAL"):
            alerts = [
                {"id": "cc:quota_exhausted", "severity": severity, "message": "rate limited"},
            ]
            assert classify_alerts(alerts, FULL_SCOPE) == []

    def test_tier3_warning(self):
        alerts = [
            {"id": "queue:deferred_work", "severity": "WARNING", "message": "Queue deep"},
        ]
        alarms = classify_alerts(alerts, FULL_SCOPE)
        assert len(alarms) == 1
        assert alarms[0].tier == 3

    def test_mixed_tiers_sorted(self):
        alerts = [
            {"id": "queue:deferred_work", "severity": "WARNING", "message": "Minor"},
            {"id": "service:watchdog_blind", "severity": "WARNING", "message": "Watchdog"},
            {"id": "infra:disk_low", "severity": "CRITICAL", "message": "Disk"},
        ]
        alarms = classify_alerts(alerts, FULL_SCOPE)
        assert len(alarms) == 3
        # Sorted by tier (worst first)
        assert alarms[0].tier == 1
        assert alarms[1].tier == 2
        assert alarms[2].tier == 3

    def test_call_site_alerts_never_classify(self):
        """Provider call sites going down is dominated by external API
        outages the Sentinel cannot fix — unmapped by design (user decision
        2026-07-04). Any severity.
        """
        for severity in ("WARNING", "CRITICAL"):
            alerts = [
                {
                    "id": "call_site:33_skill_refiner",
                    "severity": severity,
                    "message": "Call site 33_skill_refiner is DOWN (all providers exhausted)",
                },
            ]
            assert classify_alerts(alerts, FULL_SCOPE) == []

    def test_info_severity_ignored(self):
        alerts = [
            {"id": "genesis:update_failed", "severity": "INFO", "message": "All good"},
        ]
        assert classify_alerts(alerts, FULL_SCOPE) == []


# The ratified scope boundary (user decision 2026-07-04). IN = internal,
# remediable, wakes the Sentinel at CRITICAL. OUT = external / not
# remediable, never wakes it at any severity.
RATIFIED_IN = [
    "service:genesis_down",
    "service:health_data_uninitialized",
    "guardian:heartbeat_stale",
    "awareness:tick_overdue",
    "infra:disk_low",
    "infra:container_memory_high",
    "infra:qdrant_collections_missing",
    "provider:qdrant_unreachable",  # LOCAL qdrant — restartable
    "genesis:update_failed",
]
RATIFIED_OUT = [
    "backup:last_failed",
    "backup:overdue",
    "backup:not_configured",
    "provider:credit_exhaustion:deepinfra",
    "provider:embedding_failing",
    "cc:budget",
    "cc:quota_exhausted",
    "infra:ollama_model_mismatch",
    "call_site:5_deep_reflection",
]


class TestScopeBoundary:
    def test_ratified_in_classify_at_critical(self):
        for alert_id in RATIFIED_IN:
            alarms = classify_alerts(
                [{"id": alert_id, "severity": "CRITICAL", "message": f"test {alert_id}"}],
                FULL_SCOPE,
            )
            assert len(alarms) == 1, (
                f"{alert_id} did not classify — an internal CRITICAL would be "
                f"silently dropped. Map it in sentinel/remediation_map.py."
            )
            assert alarms[0].tier in (1, 2), (
                f"{alert_id} classified as Tier {alarms[0].tier}, expected 1 or 2"
            )

    def test_ratified_out_never_classify(self):
        for alert_id in RATIFIED_OUT:
            alarms = classify_alerts(
                [{"id": alert_id, "severity": "CRITICAL", "message": f"test {alert_id}"}],
                FULL_SCOPE,
            )
            assert alarms == [], (
                f"{alert_id} classified as a fire alarm — it is outside "
                f"Sentinel scope by user decision (2026-07-04). If this is "
                f"intentional, update RATIFIED_OUT with the new decision."
            )

    def test_tier1_patterns_all_mapped(self):
        """Every Tier-1 (defense failure) id must have a remediation
        mapping, or Tier-1 wake dies silently at the scope stage.
        """
        for alert_id in _TIER1_PATTERNS:
            assert required_tools(alert_id) is not None, (
                f"Tier-1 pattern {alert_id} has no remediation mapping — "
                f"defense-failure wake would be silently dropped"
            )


def _emitted_id_shapes() -> set[str]:
    """Every alert id shape the health emitter can produce, extracted
    mechanically from the source. Dynamic-suffix f-string ids are
    normalized to their prefix (text before the first ``{``).
    """
    import genesis.mcp.health.errors as errors_mod

    src = Path(errors_mod.__file__).read_text()
    raw = set(re.findall(r'(?:alert_id = |"id": )f?"([^"]+)"', src))
    shapes = set()
    for rid in raw:
        shapes.add(rid.split("{", 1)[0] if "{" in rid else rid)
    return shapes


class TestEmitterCoverage:
    """Cross-cutting coverage guardrail (same pattern as
    test_recall_inject_coverage): every alert id the emitter can produce
    must take an EXPLICIT disposition — mapped in the remediation map, or
    listed in UNMAPPED_BY_DESIGN with a reason. A new emitter that does
    neither fails here, so scope drift is impossible in either direction.
    """

    @staticmethod
    def _placed(shape: str) -> bool:
        if shape in REMEDIATION_MAP or shape in UNMAPPED_BY_DESIGN:
            return True
        for prefix in REMEDIATION_PREFIX_MAP:
            if shape.startswith(prefix) or prefix.startswith(shape):
                return True
        for key in UNMAPPED_BY_DESIGN:
            if key.endswith(":") and (shape.startswith(key) or key.startswith(shape)):
                return True
        return False

    def test_every_emitted_id_has_explicit_disposition(self):
        shapes = _emitted_id_shapes()
        assert shapes, "extraction found no alert ids — regex drifted from errors.py style"
        unplaced = sorted(s for s in shapes if not self._placed(s))
        assert not unplaced, (
            f"Alert ids with no explicit Sentinel disposition: {unplaced}. "
            f"Add each to REMEDIATION_MAP/REMEDIATION_PREFIX_MAP (wakes the "
            f"Sentinel) or UNMAPPED_BY_DESIGN with a reason (never wakes it) "
            f"in sentinel/remediation_map.py."
        )

    def test_extraction_sees_known_ids(self):
        """Canary for the regex itself — if errors.py changes its emission
        style, extraction must fail loudly, not return a shrunken set.
        """
        shapes = _emitted_id_shapes()
        for known in ("backup:last_failed", "service:genesis_down", "call_site:",
                      "provider:credit_exhaustion:", "service:health_data_uninitialized"):
            assert known in shapes, f"extraction lost {known} — update the regex"


class TestRemediationMap:
    def test_available_tools_returns_frozenset(self):
        tools = available_tools()
        assert isinstance(tools, frozenset)
        # The always-available container tools must be present everywhere.
        assert {"container.services", "container.disk_reclaim",
                "container.process_control", "container.db_local"} <= tools

    def test_detector_failure_is_unavailable(self, monkeypatch):
        """A raising detector means the tool is treated as unavailable
        (fail-closed), not a crash of the classification path.
        """
        import genesis.sentinel.remediation_map as rm

        def _boom() -> bool:
            raise RuntimeError("detector exploded")

        broken = tuple(
            rm.RemediationTool(t.id, t.description, _boom)
            if t.id == "qdrant.local" else t
            for t in rm.TOOLS
        )
        monkeypatch.setattr(rm, "TOOLS", broken)
        assert "qdrant.local" not in rm.available_tools()

    def test_is_remediable_requires_available_tool(self):
        assert is_remediable("infra:disk_low", frozenset({"container.disk_reclaim"}))
        assert not is_remediable("infra:disk_low", frozenset({"container.db_local"}))
        assert not is_remediable("not:mapped", FULL_SCOPE)

    def test_unmapped_by_design_reasons_are_substantive(self):
        for key, reason in UNMAPPED_BY_DESIGN.items():
            assert len(reason) > 30, f"UNMAPPED_BY_DESIGN[{key!r}] needs a real reason"


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
