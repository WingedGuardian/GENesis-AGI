"""Tests for RepetitionDetector — proactive procedure suggestion."""

from __future__ import annotations

import pytest

from genesis.learning.procedural.repetition_detector import (
    RepetitionDetector,
    _extract_keywords,
)


@pytest.fixture
def detector():
    return RepetitionDetector(min_overlap=3, min_cluster_size=3)


def _obs(id: str, content: str, type: str = "learning") -> dict:
    return {"id": id, "content": content, "type": type}


class TestExtractKeywords:
    def test_removes_stopwords(self):
        kw = _extract_keywords("the quick brown fox jumps over the lazy dog")
        assert "the" not in kw
        assert "over" not in kw
        assert "quick" in kw
        assert "brown" in kw

    def test_removes_short_words(self):
        kw = _extract_keywords("I am ok to go")
        assert "am" not in kw
        assert "ok" not in kw  # length 2

    def test_lowercases(self):
        kw = _extract_keywords("Python Asyncio EventLoop")
        assert "python" in kw
        assert "asyncio" in kw


class TestDetectCandidates:
    def test_insufficient_observations(self, detector):
        obs = [_obs("1", "something"), _obs("2", "something else")]
        result = detector.detect_candidates(obs, [])
        assert result == []

    def test_no_clusters(self, detector):
        """Observations with no keyword overlap should not cluster."""
        obs = [
            _obs("1", "python asyncio event loop performance"),
            _obs("2", "javascript react component rendering"),
            _obs("3", "rust borrow checker lifetime annotations"),
        ]
        result = detector.detect_candidates(obs, [])
        assert result == []

    def test_detects_cluster(self, detector):
        """Three similar observations should produce a candidate."""
        obs = [
            _obs("1", "telegram bridge connection timeout retry logic needed"),
            _obs("2", "telegram bridge timeout errors during peak hours retry"),
            _obs("3", "bridge timeout handling telegram connection retry mechanism"),
        ]
        result = detector.detect_candidates(obs, [])
        assert len(result) == 1
        assert result[0].cluster_size >= 3
        assert len(result[0].observation_ids) >= 3

    def test_dedup_against_existing_procedure(self, detector):
        """Cluster matching an existing procedure should be filtered out."""
        obs = [
            _obs("1", "telegram bridge connection timeout retry logic needed"),
            _obs("2", "telegram bridge timeout errors during peak hours retry"),
            _obs("3", "bridge timeout handling telegram connection retry mechanism"),
        ]
        procedures = [{
            "task_type": "telegram-bridge-timeout",
            "principle": "Handle telegram bridge timeout with retry",
            "context_tags": ["telegram", "bridge", "timeout", "retry"],
        }]
        result = detector.detect_candidates(obs, procedures)
        assert result == []

    def test_mixed_clusters(self, detector):
        """Multiple clusters should produce multiple candidates."""
        obs = [
            # Cluster A: telegram timeout
            _obs("1", "telegram bridge connection timeout retry logic needed"),
            _obs("2", "telegram bridge timeout errors during peak hours retry"),
            _obs("3", "bridge timeout handling telegram connection retry mechanism"),
            # Cluster B: memory consolidation
            _obs("4", "memory observation consolidation duplicate detection needed"),
            _obs("5", "observation consolidation memory cleanup duplicate entries"),
            _obs("6", "duplicate observation detection memory consolidation process"),
            # Noise
            _obs("7", "unrelated random topic about cooking recipes"),
        ]
        result = detector.detect_candidates(obs, [])
        assert len(result) == 2

    def test_shared_keywords_populated(self, detector):
        obs = [
            _obs("1", "database migration schema version tracking needed"),
            _obs("2", "schema migration database version control tracking"),
            _obs("3", "tracking database schema migration version updates"),
        ]
        result = detector.detect_candidates(obs, [])
        assert len(result) == 1
        assert len(result[0].shared_keywords) > 0

    def test_sample_contents_truncated(self, detector):
        long_content = "x" * 200
        obs = [
            _obs("1", f"telegram bridge retry {long_content}"),
            _obs("2", f"telegram bridge retry {long_content}"),
            _obs("3", f"telegram bridge retry {long_content}"),
        ]
        result = detector.detect_candidates(obs, [])
        if result:
            for sample in result[0].sample_contents:
                assert len(sample) <= 100

    def test_empty_observations(self, detector):
        result = detector.detect_candidates([], [])
        assert result == []

    def test_custom_thresholds(self):
        detector = RepetitionDetector(min_overlap=1, min_cluster_size=2)
        obs = [
            _obs("1", "python error handling"),
            _obs("2", "python exception handling"),
        ]
        result = detector.detect_candidates(obs, [])
        assert len(result) == 1
