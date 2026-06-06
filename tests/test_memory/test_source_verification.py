"""Tests for extraction source-overlap verification."""

from __future__ import annotations

import pytest

from genesis.memory.source_verification import (
    OverlapResult,
    compute_jaccard,
    verify_source_overlap,
)


class TestVerifySourceOverlap:
    """Tests for the structural source-overlap check."""

    def test_high_overlap_passes(self):
        """Extraction content that appears in source passes."""
        source = "We evaluated Agentmail for outreach. It handles email delivery for AI agents."
        extraction = "Agentmail evaluated for Genesis outreach — handles email delivery for AI agents."
        result = verify_source_overlap(extraction, source)
        assert result.verified is True
        assert result.overlap >= 0.4

    def test_hallucinated_extraction_fails(self):
        """Extraction with content not in source fails."""
        source = "We discussed the dashboard layout and Telegram integration."
        extraction = "Genesis has integrated with Slack for team notifications."
        result = verify_source_overlap(extraction, source)
        assert result.verified is False
        assert result.overlap < 0.4

    def test_partial_overlap_near_threshold(self):
        """Extraction with some shared terms but heavy fabrication fails."""
        source = "The dream cycle runs weekly on Sundays at 4 AM UTC."
        extraction = "The dream cycle performs adversarial review of all memories daily at midnight."
        result = verify_source_overlap(extraction, source)
        # "dream", "cycle" overlap but the rest is fabricated
        assert result.overlap < 0.4

    def test_empty_source_fails(self):
        """Empty source means no grounding possible."""
        result = verify_source_overlap("some extraction", "")
        assert result.verified is False

    def test_empty_extraction_fails(self):
        """Empty extraction is invalid."""
        result = verify_source_overlap("", "some source text")
        assert result.verified is False

    def test_custom_threshold(self):
        """Threshold is configurable."""
        source = "The memory system uses SQLite and Qdrant."
        extraction = "Memory system uses SQLite for storage."
        result_low = verify_source_overlap(extraction, source, threshold=0.3)
        result_high = verify_source_overlap(extraction, source, threshold=0.9)
        assert result_low.verified is True
        assert result_high.verified is False

    def test_stopwords_excluded(self):
        """Common words like 'the', 'is', 'a' don't count toward overlap."""
        source = "The system is a very important and critical component."
        extraction = "The system is a fundamentally broken disaster."
        result = verify_source_overlap(extraction, source)
        # Only "system", "component"/"broken" etc — low overlap
        assert result.overlap < 0.4

    def test_result_fields_populated(self):
        """OverlapResult has all expected fields."""
        source = "Genesis uses SQLite WAL mode for concurrent database access."
        extraction = "Genesis uses SQLite WAL for database access."
        result = verify_source_overlap(extraction, source)
        assert isinstance(result, OverlapResult)
        assert result.extraction_terms > 0
        assert result.matched_terms > 0
        assert 0.0 <= result.overlap <= 1.0


class TestComputeJaccard:
    """Tests for Jaccard similarity computation."""

    def test_identical_strings(self):
        """Identical strings have Jaccard 1.0."""
        text = "Genesis memory system uses SQLite WAL mode"
        assert compute_jaccard(text, text) == 1.0

    def test_high_overlap(self):
        """Similar strings have high Jaccard."""
        a = "Genesis memory system uses SQLite WAL mode for concurrent access"
        b = "Genesis memory system uses SQLite WAL for concurrent database access"
        assert compute_jaccard(a, b) >= 0.7

    def test_no_overlap(self):
        """Completely different strings have Jaccard 0.0."""
        a = "The dashboard renders health metrics via Plotly charts"
        b = "Telegram bot handles user authentication tokens"
        assert compute_jaccard(a, b) == 0.0

    def test_empty_string(self):
        """Empty strings return 0.0."""
        assert compute_jaccard("", "some text") == 0.0
        assert compute_jaccard("some text", "") == 0.0

    def test_symmetric(self):
        """Jaccard is symmetric."""
        a = "Genesis memory system"
        b = "memory system architecture"
        assert compute_jaccard(a, b) == compute_jaccard(b, a)
