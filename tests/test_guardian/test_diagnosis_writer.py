"""Tests for Guardian diagnosis result writer."""

from __future__ import annotations

import json
import time
from pathlib import Path

from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisResult, RecoveryAction
from genesis.guardian.diagnosis_writer import (
    _prune_old_findings,
    write_diagnosis_result,
)


def _make_result(**overrides) -> DiagnosisResult:
    """Create a test DiagnosisResult with sensible defaults."""
    defaults = {
        "likely_cause": "OOM kill — bridge exceeded cgroup limit",
        "confidence_pct": 85,
        "evidence": ["journal shows 'Killed process'", "memory at 99%"],
        "recommended_action": RecoveryAction.RESTART_SERVICES,
        "actions_taken": ["Took snapshot", "Restarted bridge"],
        "outcome": "resolved",
        "reasoning": "Memory exceeded 24GiB limit due to log accumulation.",
        "source": "cc",
    }
    defaults.update(overrides)
    return DiagnosisResult(**defaults)


def _make_config(tmp_path: Path) -> GuardianConfig:
    """Create a config pointing state_dir at tmp_path."""
    return GuardianConfig(state_dir=str(tmp_path))


class TestWriteDiagnosisResult:
    """Test diagnosis result file creation."""

    def test_writes_json_file(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = _make_result()
        path = write_diagnosis_result(result, config)
        assert path is not None
        assert path.exists()
        assert path.suffix == ".json"
        assert "guardian_diagnosis_" in path.name

    def test_json_structure(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = _make_result()
        path = write_diagnosis_result(result, config, outage_duration_s=180.5)
        data = json.loads(path.read_text())

        assert data["likely_cause"] == "OOM kill — bridge exceeded cgroup limit"
        assert data["confidence_pct"] == 85
        assert data["evidence"] == ["journal shows 'Killed process'", "memory at 99%"]
        assert data["recommended_action"] == "RESTART_SERVICES"
        assert data["actions_taken"] == ["Took snapshot", "Restarted bridge"]
        assert data["outcome"] == "resolved"
        assert data["source"] == "cc"
        assert data["outage_duration_s"] == 180.5
        assert "diagnosed_at" in data

    def test_creates_directory(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path / "deep" / "nested")
        result = _make_result()
        path = write_diagnosis_result(result, config)
        assert path is not None
        assert path.exists()

    def test_serializes_recovery_action_as_string(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        for action in RecoveryAction:
            result = _make_result(recommended_action=action)
            path = write_diagnosis_result(result, config)
            data = json.loads(path.read_text())
            assert isinstance(data["recommended_action"], str)
            assert data["recommended_action"] == action.value

    def test_clamps_confidence(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = _make_result(confidence_pct=150)
        path = write_diagnosis_result(result, config)
        data = json.loads(path.read_text())
        assert data["confidence_pct"] == 100

    def test_includes_outage_duration(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = _make_result()
        path = write_diagnosis_result(result, config, outage_duration_s=42.7)
        data = json.loads(path.read_text())
        assert data["outage_duration_s"] == 42.7


class TestPruneOldFindings:
    """Test file pruning."""

    def test_prunes_excess_files(self, tmp_path: Path) -> None:
        findings = tmp_path / "shared" / "findings"
        findings.mkdir(parents=True)
        for i in range(25):
            (findings / f"guardian_diagnosis_2026040{i:02d}.json").write_text("{}")
            time.sleep(0.01)  # Ensure distinct mtimes

        pruned = _prune_old_findings(findings, max_files=20)
        assert pruned == 5
        remaining = list(findings.glob("guardian_diagnosis_*.json"))
        assert len(remaining) == 20

    def test_no_prune_under_limit(self, tmp_path: Path) -> None:
        findings = tmp_path / "shared" / "findings"
        findings.mkdir(parents=True)
        for i in range(5):
            (findings / f"guardian_diagnosis_{i}.json").write_text("{}")

        pruned = _prune_old_findings(findings, max_files=20)
        assert pruned == 0

    def test_ignores_non_diagnosis_files(self, tmp_path: Path) -> None:
        findings = tmp_path / "shared" / "findings"
        findings.mkdir(parents=True)
        (findings / "other_file.json").write_text("{}")
        (findings / "guardian_diagnosis_1.json.ingested").write_text("{}")

        pruned = _prune_old_findings(findings, max_files=0)
        assert pruned == 0  # No matching files to prune
        assert (findings / "other_file.json").exists()
        assert (findings / "guardian_diagnosis_1.json.ingested").exists()
