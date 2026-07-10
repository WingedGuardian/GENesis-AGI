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
    _ensure_work_dir,
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
                ServiceInfo(name="genesis-server", active=False, sub_state="dead"),
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
            services=[ServiceInfo(name="genesis-server", active=True, sub_state="running")],
        )
        result = engine._escalate_without_cc(snap)
        assert any("Container: Running" in e for e in result.evidence)
        assert any("genesis-server" in e for e in result.evidence)
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
        briefing = "### Service Baseline\n- genesis-server: main service"
        prompt = _build_diagnosis_prompt(
            snap, "signal data", "genesis", briefing_context=briefing,
        )
        assert "Genesis Context Briefing" in prompt
        assert "genesis-server: main service" in prompt

    def test_briefing_none_same_as_no_briefing(self) -> None:
        from genesis.guardian.diagnosis import _build_diagnosis_prompt
        snap = _snap(container_status="Running")
        prompt_none = _build_diagnosis_prompt(snap, "", "genesis", briefing_context=None)
        prompt_default = _build_diagnosis_prompt(snap, "", "genesis")
        assert prompt_none == prompt_default

    def test_prompt_is_propose_only(self) -> None:
        """Propose-only firewall: the CC diagnosis prompt must forbid acting and
        instruct CC to propose via recommended_action only."""
        from genesis.guardian.diagnosis import _build_diagnosis_prompt
        snap = _snap(container_status="Stopped")
        prompt = _build_diagnosis_prompt(snap, "signal data", "genesis")
        assert "DO NOT execute" in prompt


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

    def test_parse_defaults_outcome_to_proposed(self, engine: DiagnosisEngine) -> None:
        """Propose-only: a CC response with no explicit `outcome` parses as
        'proposed' (CC investigates and proposes; it never self-resolves)."""
        raw = json.dumps({
            "result": json.dumps({
                "likely_cause": "bridge crash",
                "confidence_pct": 70,
                "evidence": [],
                "recommended_action": "RESTART_SERVICES",
                "reasoning": "crash loop",
            }),
        })
        result = engine._parse_cc_response(raw)
        assert result is not None
        assert result.outcome == "proposed"


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
    async def test_cc_parse_failure_escalates(self, engine: DiagnosisEngine) -> None:
        from genesis.guardian.diagnosis import CCDiagnosisError
        with patch.object(
            engine, "_diagnose_with_cc",
            side_effect=CCDiagnosisError("No valid JSON diagnosis found"),
        ):
            snap = _snap(container_status="Running")
            result = await engine.diagnose(snap)
        assert result.source == "cc_unavailable"
        assert result.recommended_action == RecoveryAction.ESCALATE
        assert "CCDiagnosisError" in result.cc_failure_reason


# ── RecoveryAction enum ──────────────────────────────────────────────────


class TestRecoveryAction:

    def test_all_actions_defined(self) -> None:
        actions = list(RecoveryAction)
        assert len(actions) == 7
        assert RecoveryAction.RESTART_SERVICES in actions
        assert RecoveryAction.IO_TRIAGE in actions
        assert RecoveryAction.ESCALATE in actions

    def test_escalation_order(self) -> None:
        ordered = [
            RecoveryAction.RESTART_SERVICES,
            RecoveryAction.IO_TRIAGE,
            RecoveryAction.RESOURCE_CLEAR,
            RecoveryAction.REVERT_CODE,
            RecoveryAction.RESTART_CONTAINER,
            RecoveryAction.SNAPSHOT_ROLLBACK,
            RecoveryAction.ESCALATE,
        ]
        assert all(a in RecoveryAction for a in ordered)


def test_prompt_includes_essential_knowledge(tmp_path, monkeypatch):
    """Essential knowledge file should appear in guardian diagnosis prompt."""
    from pathlib import Path

    from genesis.guardian.diagnosis import _build_diagnosis_prompt

    ek_dir = tmp_path / ".genesis"
    ek_dir.mkdir()
    (ek_dir / "essential_knowledge.md").write_text("Wing: infrastructure\nActive: approval gate fix")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    snap = _snap(container_status="Running")
    prompt = _build_diagnosis_prompt(snap, "test signal", "genesis")
    assert "Essential Knowledge" in prompt
    assert "approval gate fix" in prompt


# ── _ensure_work_dir — guardian brain survives an uncreatable work_dir ──────
def test_ensure_work_dir_uses_configured_when_creatable(tmp_path):
    configured = tmp_path / "guardian-snapshots" / "cc-sessions"
    got = _ensure_work_dir(configured, fallback=tmp_path / "fb")
    assert got == configured and configured.is_dir()


def test_ensure_work_dir_falls_back_when_configured_uncreatable(tmp_path):
    # A file where a parent dir is expected → mkdir(parents=True) raises OSError
    # (NotADirectoryError). Mirrors a root-owned/misconfigured cc.work_dir.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    configured = blocker / "cc-sessions"
    fallback = tmp_path / "state" / "cc-sessions"
    got = _ensure_work_dir(configured, fallback=fallback)
    assert got == fallback and fallback.is_dir()
    assert not configured.exists()  # configured was never created


def test_ensure_work_dir_propagates_when_fallback_also_uncreatable(tmp_path):
    # No writable dir anywhere → OSError propagates (alert-only is the honest
    # degradation; we must NOT silently return an unusable path).
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    with pytest.raises(OSError):
        _ensure_work_dir(blocker / "wd", fallback=blocker / "fb")


def _stub_claude(tmp_path, body: str):
    """A stub `claude` that prints ``body`` for `auth status --json`."""
    stub = tmp_path / "claude"
    stub.write_text(f"#!/bin/sh\ncat <<'STUBEOF'\n{body}\nSTUBEOF\n")
    stub.chmod(0o755)
    return str(stub)


def _engine_with_token(tmp_path, *, token: bool) -> DiagnosisEngine:
    cfg = GuardianConfig()
    cfg.state_dir = str(tmp_path / "state")
    if token:
        d = tmp_path / "state" / "shared" / "guardian"
        d.mkdir(parents=True)
        (d / "cc_oauth_token.env").write_text(
            "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SYNTHETIC\n"
            "GENESIS_CC_TOKEN_CREATED_AT=1700000000\n",
        )
    return DiagnosisEngine(cfg)


class TestResolveCCEnv:
    """FALLBACK-ONLY setup-token injection for the CC recovery brain."""

    @pytest.mark.asyncio
    async def test_logged_in_true_never_injects(self, tmp_path):
        # Working login present → inherit env (None), even WITH a token synced.
        cc = _stub_claude(tmp_path, '{"loggedIn": true}')
        engine = _engine_with_token(tmp_path, token=True)
        assert await engine._resolve_cc_env(cc) is None

    @pytest.mark.asyncio
    async def test_dead_login_with_token_injects_and_preserves_env(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("CANARY_ENV", "keepme")
        cc = _stub_claude(tmp_path, '{"loggedIn": false}')
        engine = _engine_with_token(tmp_path, token=True)
        env = await engine._resolve_cc_env(cc)
        assert isinstance(env, dict)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-SYNTHETIC"
        assert env["CANARY_ENV"] == "keepme"  # inherited env preserved

    @pytest.mark.asyncio
    async def test_dead_login_no_token_inherits(self, tmp_path):
        cc = _stub_claude(tmp_path, '{"loggedIn": false}')
        engine = _engine_with_token(tmp_path, token=False)
        assert await engine._resolve_cc_env(cc) is None

    @pytest.mark.asyncio
    async def test_unparseable_status_never_injects(self, tmp_path):
        # Ambiguous auth status + token present → still inherit (never inject).
        cc = _stub_claude(tmp_path, "not json at all")
        engine = _engine_with_token(tmp_path, token=True)
        assert await engine._resolve_cc_env(cc) is None

    @pytest.mark.asyncio
    async def test_missing_binary_never_raises(self, tmp_path):
        engine = _engine_with_token(tmp_path, token=True)
        assert await engine._resolve_cc_env(str(tmp_path / "nonexistent")) is None

    @pytest.mark.asyncio
    async def test_non_object_json_never_injects(self, tmp_path):
        # Valid JSON but not an object (null) → .get would raise; must be caught
        # as ambiguity → inherit env, even with a token present.
        cc = _stub_claude(tmp_path, "null")
        engine = _engine_with_token(tmp_path, token=True)
        assert await engine._resolve_cc_env(cc) is None
