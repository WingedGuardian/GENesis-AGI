"""Tests for adversarial review of dream cycle synthesis."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.memory.adversarial_review import (
    CALL_SITE_ENTITY,
    CALL_SITE_SYNTHESIS,
    AdversarialVerdict,
    SynthesisBlockedError,
    _parse_verdict,
    check_entity_duplicate,
    check_synthesis_faithfulness,
)


class TestParseVerdict:
    """Tests for verdict parsing."""

    def test_pass_verdict(self):
        raw = json.dumps({"verdict": "PASS"})
        result = _parse_verdict(raw)
        assert result.passed is True
        assert result.missing == []

    def test_fail_verdict_with_missing(self):
        raw = json.dumps({
            "verdict": "FAIL",
            "missing": ["date of PR merge", "specific version number"],
        })
        result = _parse_verdict(raw)
        assert result.passed is False
        assert len(result.missing) == 2

    def test_malformed_json_defaults_fail(self):
        """Parse errors default to FAIL (fail-safe)."""
        result = _parse_verdict("not json at all")
        assert result.passed is False

    def test_json_in_markdown_fence(self):
        raw = '```json\n{"verdict": "PASS"}\n```'
        result = _parse_verdict(raw)
        assert result.passed is True

    def test_empty_string_defaults_fail(self):
        result = _parse_verdict("")
        assert result.passed is False

    def test_unknown_verdict_defaults_fail(self):
        raw = json.dumps({"verdict": "MAYBE"})
        result = _parse_verdict(raw)
        assert result.passed is False


class TestCallSiteIds:
    """Verify call site IDs match naming convention."""

    def test_synthesis_call_site(self):
        assert CALL_SITE_SYNTHESIS == "dream_cycle_synthesis_challenge"

    def test_entity_call_site(self):
        assert CALL_SITE_ENTITY == "dream_cycle_entity_challenge"


class TestSynthesisBlockedError:
    """Tests for the SynthesisBlockedError exception."""

    def test_with_missing_items(self):
        err = SynthesisBlockedError(missing=["date", "version"])
        assert "date" in str(err)
        assert "version" in str(err)

    def test_with_error(self):
        err = SynthesisBlockedError(error="timeout")
        assert "timeout" in str(err)

    def test_is_exception(self):
        assert issubclass(SynthesisBlockedError, Exception)


class TestCheckSynthesisFaithfulness:
    """Tests for the full adversarial review flow."""

    @pytest.mark.asyncio
    async def test_pass_allows_deprecation(self):
        """When adversary says PASS, synthesis proceeds."""
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=MagicMock(
            success=True,
            content=json.dumps({"verdict": "PASS"}),
        ))

        originals = [
            {"content": "Genesis uses SQLite WAL", "confidence": 0.8},
            {"content": "Genesis database is SQLite with WAL mode", "confidence": 0.7},
        ]
        synthesis_text = "Genesis uses SQLite with WAL mode for its database."

        result = await check_synthesis_faithfulness(
            router=router, originals=originals, synthesis_text=synthesis_text,
        )
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_fail_blocks_deprecation(self):
        """When adversary says FAIL, synthesis is blocked."""
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=MagicMock(
            success=True,
            content=json.dumps({
                "verdict": "FAIL",
                "missing": ["specific WAL configuration details"],
            }),
        ))

        originals = [
            {"content": "Genesis uses SQLite WAL with journal_mode pragma", "confidence": 0.8},
        ]
        synthesis_text = "Genesis uses SQLite."

        result = await check_synthesis_faithfulness(
            router=router, originals=originals, synthesis_text=synthesis_text,
        )
        assert result.passed is False
        assert len(result.missing) >= 1

    @pytest.mark.asyncio
    async def test_router_error_defaults_fail(self):
        """Router errors default to FAIL (fail-safe)."""
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=MagicMock(
            success=False, error="provider timeout",
        ))

        result = await check_synthesis_faithfulness(
            router=router, originals=[{"content": "x"}], synthesis_text="y",
        )
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_router_exception_defaults_fail(self):
        """Router exceptions default to FAIL (fail-safe)."""
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=RuntimeError("connection refused"))

        result = await check_synthesis_faithfulness(
            router=router, originals=[{"content": "x"}], synthesis_text="y",
        )
        assert result.passed is False


class TestCheckEntityDuplicate:
    """Tests for entity duplicate second opinion."""

    @pytest.mark.asyncio
    async def test_confirms_duplicate(self):
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=MagicMock(
            success=True,
            content=json.dumps({"relationship": "duplicate", "reasoning": "same info"}),
        ))
        result = await check_entity_duplicate(
            router=router, content_a="memory A", content_b="memory B",
        )
        assert result["relationship"] == "duplicate"

    @pytest.mark.asyncio
    async def test_overrides_to_distinct(self):
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=MagicMock(
            success=True,
            content=json.dumps({"relationship": "distinct", "reasoning": "different events"}),
        ))
        result = await check_entity_duplicate(
            router=router, content_a="memory A", content_b="memory B",
        )
        assert result["relationship"] == "distinct"

    @pytest.mark.asyncio
    async def test_error_defaults_distinct(self):
        """Errors default to distinct (fail-safe — preserve both)."""
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=MagicMock(
            success=False, error="timeout",
        ))
        result = await check_entity_duplicate(
            router=router, content_a="a", content_b="b",
        )
        assert result["relationship"] == "distinct"

    @pytest.mark.asyncio
    async def test_parse_error_defaults_distinct(self):
        """Parse errors default to distinct."""
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=MagicMock(
            success=True, content="not json",
        ))
        result = await check_entity_duplicate(
            router=router, content_a="a", content_b="b",
        )
        assert result["relationship"] == "distinct"
