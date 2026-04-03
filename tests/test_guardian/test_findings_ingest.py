"""Tests for Guardian findings ingest — shared filesystem → Genesis DB."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.findings_ingest import ingest_guardian_findings

# Patch targets — the actual CRUD modules
_OBS_CREATE = "genesis.db.crud.observations.create"
_EVT_INSERT = "genesis.db.crud.events.insert"


def _write_finding(findings_dir: Path, filename: str = "guardian_diagnosis_20260403T163000.json", **overrides) -> Path:
    """Write a test diagnosis JSON file."""
    data = {
        "diagnosed_at": "2026-04-03T16:30:00Z",
        "likely_cause": "OOM kill — bridge exceeded cgroup limit",
        "confidence_pct": 85,
        "evidence": ["journal shows 'Killed process'"],
        "recommended_action": "RESTART_SERVICES",
        "actions_taken": ["Restarted bridge"],
        "outcome": "resolved",
        "reasoning": "Memory exceeded limit.",
        "source": "cc",
        "outage_duration_s": 180.0,
    }
    data.update(overrides)
    path = findings_dir / filename
    path.write_text(json.dumps(data))
    return path


class TestIngestGuardianFindings:
    """Test the main ingest function."""

    @pytest.mark.asyncio
    async def test_ingests_valid_finding(self, tmp_path: Path) -> None:
        _write_finding(tmp_path)
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")),
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        assert count == 1

    @pytest.mark.asyncio
    async def test_renames_ingested_file(self, tmp_path: Path) -> None:
        path = _write_finding(tmp_path)
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")),
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        assert not path.exists()
        assert path.with_suffix(".json.ingested").exists()

    @pytest.mark.asyncio
    async def test_skips_low_confidence(self, tmp_path: Path) -> None:
        path = _write_finding(tmp_path, confidence_pct=30)
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")) as mock_obs,
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        assert count == 0
        mock_obs.assert_not_called()
        # Low-confidence files renamed to .skipped (not .ingested)
        assert path.with_suffix(".json.skipped").exists()

    @pytest.mark.asyncio
    async def test_skips_already_ingested(self, tmp_path: Path) -> None:
        # Write an already-ingested file (wrong suffix for glob)
        (tmp_path / "guardian_diagnosis_20260403.json.ingested").write_text("{}")
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")) as mock_obs,
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        assert count == 0
        mock_obs.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self, tmp_path: Path) -> None:
        (tmp_path / "guardian_diagnosis_bad.json").write_text("not json{{{")
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")) as mock_obs,
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        assert count == 0
        mock_obs.assert_not_called()
        # Should be renamed to .corrupt
        assert (tmp_path / "guardian_diagnosis_bad.json.corrupt").exists()

    @pytest.mark.asyncio
    async def test_handles_missing_dir(self) -> None:
        count = await ingest_guardian_findings(
            AsyncMock(), findings_dir=Path("/nonexistent/path"),
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_handles_empty_dir(self, tmp_path: Path) -> None:
        count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)
        assert count == 0

    @pytest.mark.asyncio
    async def test_handles_missing_required_fields(self, tmp_path: Path) -> None:
        # Missing likely_cause
        path = tmp_path / "guardian_diagnosis_incomplete.json"
        path.write_text(json.dumps({"confidence_pct": 90, "outcome": "resolved"}))
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")) as mock_obs,
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        assert count == 0
        mock_obs.assert_not_called()
        assert path.with_suffix(".json.corrupt").exists()


class TestPriorityMapping:
    """Test confidence → priority mapping."""

    @pytest.mark.asyncio
    async def test_high_confidence_high_priority(self, tmp_path: Path) -> None:
        _write_finding(tmp_path, confidence_pct=90)
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")) as mock_obs,
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        call_kwargs = mock_obs.call_args[1]
        assert call_kwargs["priority"] == "high"

    @pytest.mark.asyncio
    async def test_medium_confidence_medium_priority(self, tmp_path: Path) -> None:
        _write_finding(tmp_path, confidence_pct=65)
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")) as mock_obs,
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        call_kwargs = mock_obs.call_args[1]
        assert call_kwargs["priority"] == "medium"

    @pytest.mark.asyncio
    async def test_low_confidence_low_priority(self, tmp_path: Path) -> None:
        _write_finding(tmp_path, confidence_pct=55)
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")) as mock_obs,
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        call_kwargs = mock_obs.call_args[1]
        assert call_kwargs["priority"] == "low"


class TestEventCreation:
    """Test event severity mapping."""

    @pytest.mark.asyncio
    async def test_resolved_creates_info_event(self, tmp_path: Path) -> None:
        _write_finding(tmp_path, outcome="resolved")
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")),
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")) as mock_evt,
        ):
            await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        call_kwargs = mock_evt.call_args[1]
        assert call_kwargs["severity"] == "info"
        assert call_kwargs["subsystem"] == "guardian"
        assert call_kwargs["event_type"] == "diagnosis.ingested"

    @pytest.mark.asyncio
    async def test_escalate_creates_error_event(self, tmp_path: Path) -> None:
        _write_finding(tmp_path, outcome="escalate", confidence_pct=80)
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")),
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")) as mock_evt,
        ):
            await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        call_kwargs = mock_evt.call_args[1]
        assert call_kwargs["severity"] == "error"

    @pytest.mark.asyncio
    async def test_partially_resolved_creates_warning_event(self, tmp_path: Path) -> None:
        _write_finding(tmp_path, outcome="partially_resolved")
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")),
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")) as mock_evt,
        ):
            await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        call_kwargs = mock_evt.call_args[1]
        assert call_kwargs["severity"] == "warning"


class TestMultipleFindings:
    """Test processing multiple files in one pass."""

    @pytest.mark.asyncio
    async def test_ingests_multiple_files(self, tmp_path: Path) -> None:
        _write_finding(tmp_path, filename="guardian_diagnosis_20260403T100000.json")
        _write_finding(
            tmp_path,
            filename="guardian_diagnosis_20260403T110000.json",
            likely_cause="Disk full",
            confidence_pct=75,
        )
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")),
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        assert count == 2
        # Both should be renamed
        assert not list(tmp_path.glob("guardian_diagnosis_*.json"))
        assert len(list(tmp_path.glob("*.ingested"))) == 2

    @pytest.mark.asyncio
    async def test_bad_file_doesnt_block_good_file(self, tmp_path: Path) -> None:
        (tmp_path / "guardian_diagnosis_20260403T090000.json").write_text("bad{json")
        _write_finding(tmp_path, filename="guardian_diagnosis_20260403T100000.json")
        with (
            patch(_OBS_CREATE, new=AsyncMock(return_value="obs-123")),
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        assert count == 1
        assert (tmp_path / "guardian_diagnosis_20260403T090000.json.corrupt").exists()
        assert (tmp_path / "guardian_diagnosis_20260403T100000.json.ingested").exists()

    @pytest.mark.asyncio
    async def test_observation_failure_skips_file(self, tmp_path: Path) -> None:
        _write_finding(tmp_path)
        with (
            patch(_OBS_CREATE, new=AsyncMock(side_effect=Exception("DB locked"))),
            patch(_EVT_INSERT, new=AsyncMock(return_value="evt-456")),
        ):
            count = await ingest_guardian_findings(AsyncMock(), findings_dir=tmp_path)

        # File not renamed — will be retried next tick
        assert count == 0
        assert (tmp_path / "guardian_diagnosis_20260403T163000.json").exists()
