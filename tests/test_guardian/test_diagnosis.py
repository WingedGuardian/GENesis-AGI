"""Tests for Guardian diagnosis engine."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from genesis.guardian.collector import (
    DiagnosticSnapshot,
    DiskInfo,
    ServiceInfo,
)
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import (
    DiagnosisEngine,
    RecoveryAction,
)


@pytest.fixture
def config() -> GuardianConfig:
    return GuardianConfig()


@pytest.fixture
def engine(config: GuardianConfig) -> DiagnosisEngine:
    return DiagnosisEngine(config)


def _snap(**kwargs) -> DiagnosticSnapshot:
    return DiagnosticSnapshot(**kwargs)


# ── CC unavailable — always ESCALATE ────────────────────────────────────


class TestCCUnavailableEscalation:
    """Without CC, Guardian takes NO recovery actions. Always ESCALATE."""

    def test_always_escalates(self, engine: DiagnosisEngine) -> None:
        snap = _snap(container_status="Running")
        result = engine._escalate_without_cc(snap)
        assert result.recommended_action == RecoveryAction.ESCALATE
        assert result.confidence_pct == 0
        assert result.source == "cc_unavailable"

    def test_escalates_even_on_oom(self, engine: DiagnosisEngine) -> None:
        """OOM signature in journal — still ESCALATE. Signal could be false."""
        snap = _snap(journal_recent="Mar 25 12:00 kernel: Killed process 1234")
        result = engine._escalate_without_cc(snap)
        assert result.recommended_action == RecoveryAction.ESCALATE
        assert "OOM kill signature" in result.evidence[-1]

    def test_escalates_even_on_tmp_full(self, engine: DiagnosisEngine) -> None:
        snap = _snap(
            container_status="Running",
            disks=[DiskInfo(mount="/tmp", total_mb=512, used_mb=490, avail_mb=22, usage_pct=95.7)],
        )
        result = engine._escalate_without_cc(snap)
        assert result.recommended_action == RecoveryAction.ESCALATE

    def test_escalates_even_on_container_stopped(self, engine: DiagnosisEngine) -> None:
        """Container appears stopped — still ESCALATE. Could be incus bug."""
        snap = _snap(container_status="Stopped")
        result = engine._escalate_without_cc(snap)
        assert result.recommended_action == RecoveryAction.ESCALATE

    def test_escalates_even_on_all_services_down(self, engine: DiagnosisEngine) -> None:
        snap = _snap(
            container_status="Running",
            services=[
                ServiceInfo(name="genesis-bridge", active=False, sub_state="dead"),
                ServiceInfo(name="qdrant", active=False, sub_state="dead"),
            ],
        )
        result = engine._escalate_without_cc(snap)
        assert result.recommended_action == RecoveryAction.ESCALATE

    def test_includes_diagnostic_evidence(self, engine: DiagnosisEngine) -> None:
        """Evidence should include all collected diagnostic data."""
        snap = _snap(
            container_status="Running",
            disks=[DiskInfo(mount="/", total_mb=140000, used_mb=70000, avail_mb=70000, usage_pct=50.0)],
            services=[ServiceInfo(name="genesis-bridge", active=True, sub_state="running")],
        )
        result = engine._escalate_without_cc(snap)
        assert any("Container: Running" in e for e in result.evidence)
        assert any("genesis-bridge" in e for e in result.evidence)
        assert any("Disk /:" in e for e in result.evidence)


# ── Briefing integration ───────────────────────────────────────────────


class TestBriefingIntegration:
    """Test that briefing content is injected into the CC prompt."""

    def test_prompt_without_briefing(self) -> None:
        from genesis.guardian.diagnosis import _build_diagnosis_prompt
        snap = _snap(container_status="Running")
        prompt = _build_diagnosis_prompt(snap, "signal data", "genesis")
        assert "Genesis Context Briefing" not in prompt
        assert "Genesis Guardian" in prompt

    def test_prompt_with_briefing(self) -> None:
        from genesis.guardian.diagnosis import _build_diagnosis_prompt
        snap = _snap(container_status="Stopped")
        briefing = "### Service Baseline\n- genesis-bridge: main service"
        prompt = _build_diagnosis_prompt(
            snap, "signal data", "genesis", briefing_context=briefing,
        )
        assert "Genesis Context Briefing" in prompt
        assert "genesis-bridge: main service" in prompt

    def test_briefing_none_same_as_no_briefing(self) -> None:
        from genesis.guardian.diagnosis import _build_diagnosis_prompt
        snap = _snap(container_status="Running")
        prompt_none = _build_diagnosis_prompt(snap, "", "genesis", briefing_context=None)
        prompt_default = _build_diagnosis_prompt(snap, "", "genesis")
        assert prompt_none == prompt_default


# ── CC response parsing ─────────────────────────────────────────────────


class TestCCResponseParsing:

    def test_parse_direct_json(self, engine: DiagnosisEngine) -> None:
        raw = json.dumps({
            "result": json.dumps({
                "likely_cause": "OOM kill",
                "confidence_pct": 85,
                "evidence": ["memory at 95%"],
                "recommended_action": "RESTART_CONTAINER",
                "reasoning": "High memory pressure",
            }),
        })
        result = engine._parse_cc_response(raw)
        assert result is not None
        assert result.likely_cause == "OOM kill"
        assert result.recommended_action == RecoveryAction.RESTART_CONTAINER
        assert result.source == "cc"

    def test_parse_content_array(self, engine: DiagnosisEngine) -> None:
        inner = json.dumps({
            "likely_cause": "Bridge crash",
            "confidence_pct": 75,
            "evidence": ["NRestarts=5"],
            "recommended_action": "RESTART_SERVICES",
            "reasoning": "Crash loop detected",
        })
        raw = json.dumps({
            "content": [{"type": "text", "text": inner}],
        })
        result = engine._parse_cc_response(raw)
        assert result is not None
        assert result.likely_cause == "Bridge crash"
        assert result.recommended_action == RecoveryAction.RESTART_SERVICES

    def test_parse_unknown_action_defaults_to_escalate(self, engine: DiagnosisEngine) -> None:
        raw = json.dumps({
            "result": json.dumps({
                "likely_cause": "Unknown",
                "confidence_pct": 30,
                "evidence": [],
                "recommended_action": "DESTROY_EVERYTHING",
                "reasoning": "Bad idea",
            }),
        })
        result = engine._parse_cc_response(raw)
        assert result is not None
        assert result.recommended_action == RecoveryAction.ESCALATE

    def test_parse_invalid_json(self, engine: DiagnosisEngine) -> None:
        result = engine._parse_cc_response("not json at all")
        assert result is None

    def test_parse_markdown_fenced(self, engine: DiagnosisEngine) -> None:
        inner = '```json\n{"likely_cause":"test","confidence_pct":50,"evidence":[],"recommended_action":"ESCALATE","reasoning":"test"}\n```'
        raw = json.dumps({"result": inner})
        result = engine._parse_cc_response(raw)
        assert result is not None
        assert result.likely_cause == "test"


# ── Full diagnosis flow ──────────────────────────────────────────────────


class TestDiagnoseFlow:

    @pytest.mark.asyncio
    async def test_cc_disabled_escalates(self) -> None:
        config = GuardianConfig()
        config.cc.enabled = False
        engine = DiagnosisEngine(config)

        snap = _snap(container_status="Stopped")
        result = await engine.diagnose(snap)
        assert result.source == "cc_unavailable"
        assert result.recommended_action == RecoveryAction.ESCALATE

    @pytest.mark.asyncio
    async def test_cc_failure_escalates(self, engine: DiagnosisEngine) -> None:
        with patch.object(
            engine, "_diagnose_with_cc", side_effect=RuntimeError("CC broke"),
        ):
            snap = _snap(container_status="Stopped")
            result = await engine.diagnose(snap)
        assert result.source == "cc_unavailable"
        assert result.recommended_action == RecoveryAction.ESCALATE

    @pytest.mark.asyncio
    async def test_cc_returns_none_escalates(self, engine: DiagnosisEngine) -> None:
        with patch.object(engine, "_diagnose_with_cc", return_value=None):
            snap = _snap(container_status="Running")
            result = await engine.diagnose(snap)
        assert result.source == "cc_unavailable"
        assert result.recommended_action == RecoveryAction.ESCALATE


# ── RecoveryAction enum ──────────────────────────────────────────────────


class TestRecoveryAction:

    def test_all_actions_defined(self) -> None:
        actions = list(RecoveryAction)
        assert len(actions) == 6
        assert RecoveryAction.RESTART_SERVICES in actions
        assert RecoveryAction.ESCALATE in actions

    def test_escalation_order(self) -> None:
        ordered = [
            RecoveryAction.RESTART_SERVICES,
            RecoveryAction.RESOURCE_CLEAR,
            RecoveryAction.REVERT_CODE,
            RecoveryAction.RESTART_CONTAINER,
            RecoveryAction.SNAPSHOT_ROLLBACK,
            RecoveryAction.ESCALATE,
        ]
        assert all(a in RecoveryAction for a in ordered)
