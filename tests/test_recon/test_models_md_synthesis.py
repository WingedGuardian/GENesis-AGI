"""Tests for ModelsMdSynthesisJob — weekly models.md synthesis."""

import pytest

from genesis.recon.models_md_synthesis import ModelsMdSynthesisJob


class TestParseBody:
    """Test finding body parsing (prefix + JSON format)."""

    def test_standard_body(self):
        body = 'Model Intelligence\n\n{"type": "pricing_change", "model": "test"}'
        result = ModelsMdSynthesisJob._parse_body(body)
        assert result == {"type": "pricing_change", "model": "test"}

    def test_json_only(self):
        body = '{"type": "new_free_model", "api_id": "test/model"}'
        result = ModelsMdSynthesisJob._parse_body(body)
        assert result == {"type": "new_free_model", "api_id": "test/model"}

    def test_no_json(self):
        assert ModelsMdSynthesisJob._parse_body("plain text no json") is None

    def test_invalid_json(self):
        assert ModelsMdSynthesisJob._parse_body("{invalid json}") is None


class TestSerializeFindings:
    """Test finding serialization for LLM prompt."""

    def test_compact_output(self):
        findings = [
            {"type": "pricing_change", "title": "Model intelligence: pricing_change — gpt-5", "model": "gpt-5"},
        ]
        result = ModelsMdSynthesisJob._serialize_findings(findings)
        assert "pricing_change — gpt-5 (pricing_change)" in result
        assert "```json" in result
        assert '"model": "gpt-5"' in result

    def test_strips_prefix(self):
        findings = [{"type": "new_free_model", "title": "Model intelligence: new — test"}]
        result = ModelsMdSynthesisJob._serialize_findings(findings)
        assert "Model intelligence: " not in result
        assert "new — test" in result

    def test_empty_findings(self):
        assert ModelsMdSynthesisJob._serialize_findings([]) == ""


class TestValidateOutput:
    """Test output validation gates."""

    MINIMAL_VALID = (
        "<!-- header -->\n"
        "## THE HEAVY LIFTERS\nContent\n"
        "## THE ALL-ROUNDERS\nContent\n"
        "## THE SPECIALISTS\nContent\n"
    )

    def test_valid_output(self):
        original = self.MINIMAL_VALID
        # Same-ish length output with all markers
        assert ModelsMdSynthesisJob._validate_output(original, original) is None

    def test_empty_output(self):
        assert ModelsMdSynthesisJob._validate_output("", "original") == "empty output"

    def test_missing_marker(self):
        output = "## THE HEAVY LIFTERS\n## THE ALL-ROUNDERS\n"
        err = ModelsMdSynthesisJob._validate_output(output, self.MINIMAL_VALID)
        assert err is not None
        assert "THE SPECIALISTS" in err

    def test_too_short(self):
        output = self.MINIMAL_VALID
        # Original is 3x longer
        original = self.MINIMAL_VALID * 3
        err = ModelsMdSynthesisJob._validate_output(output, original)
        assert err is not None
        assert "<50%" in err

    def test_too_long(self):
        output = self.MINIMAL_VALID * 5
        original = self.MINIMAL_VALID
        err = ModelsMdSynthesisJob._validate_output(output, original)
        assert err is not None
        assert ">200%" in err


class TestQueryFindings:
    """Test finding query and type filtering."""

    @pytest.mark.asyncio
    async def test_filters_to_actionable_types(self):
        """Only actionable finding types should be returned."""
        from unittest.mock import AsyncMock, MagicMock

        # Mock DB with mixed finding types
        findings_data = [
            ("concept1", 'prefix\n\n{"type": "pricing_change", "model": "test"}', "2026-05-22"),
            ("concept2", 'prefix\n\n{"type": "new_model", "api_id": "test/bulk"}', "2026-05-22"),
            ("concept3", 'prefix\n\n{"type": "new_free_model", "api_id": "test/free"}', "2026-05-22"),
            ("concept4", 'prefix\n\n{"type": "benchmark_unmatched", "source": "aa"}', "2026-05-22"),
        ]

        mock_cursor = MagicMock()
        mock_cursor.fetchall = AsyncMock(return_value=findings_data)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_cursor)

        job = ModelsMdSynthesisJob(db=mock_db, router=AsyncMock())
        findings = await job._query_findings()

        # Should include pricing_change and new_free_model, exclude new_model and benchmark_unmatched
        types = {f["type"] for f in findings}
        assert types == {"pricing_change", "new_free_model"}
        assert len(findings) == 2
