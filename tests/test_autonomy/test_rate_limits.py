"""Tests for genesis.autonomy.rate_limits — CC rate limit detection."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from genesis.autonomy.rate_limits import RateLimitDetector


@pytest.fixture()
def detector() -> RateLimitDetector:
    return RateLimitDetector()


class TestDetection:
    def test_no_rate_limit_on_success(self, detector: RateLimitDetector):
        result = detector.detect(exit_code=0, error_message="")
        assert result is None

    def test_no_rate_limit_normal_error(self, detector: RateLimitDetector):
        result = detector.detect(exit_code=1, error_message="File not found")
        assert result is None

    def test_detects_rate_limit_text(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Rate limit exceeded. Try again at 03:00 UTC.",
        )
        assert result is not None
        assert result.limit_type == "session"

    def test_detects_usage_limit(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="You've reached your usage limit for this session.",
        )
        assert result is not None

    def test_detects_quota_exceeded(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="API quota exceeded. Please wait.",
        )
        assert result is not None

    def test_detects_try_again(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Please try again in a few minutes.",
        )
        assert result is not None

    def test_case_insensitive(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="RATE LIMIT HIT",
        )
        assert result is not None

    def test_exit_code_detection(self):
        detector = RateLimitDetector(exit_codes=[42])
        result = detector.detect(exit_code=42, error_message="")
        assert result is not None

    def test_exit_code_no_match(self):
        detector = RateLimitDetector(exit_codes=[42])
        result = detector.detect(exit_code=1, error_message="")
        assert result is None


class TestLimitTypeClassification:
    def test_weekly(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Weekly usage limit reached.",
        )
        assert result is not None
        assert result.limit_type == "weekly"

    def test_monthly(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Monthly quota exceeded.",
        )
        assert result is not None
        assert result.limit_type == "monthly"

    def test_daily(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Daily rate limit exceeded.",
        )
        assert result is not None
        assert result.limit_type == "daily"

    def test_default_session(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Rate limit exceeded.",
        )
        assert result is not None
        assert result.limit_type == "session"


class TestResumeTimeExtraction:
    def test_extracts_time(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Rate limit exceeded. Try again at 3:00 UTC.",
        )
        assert result is not None
        assert "3:00" in result.resume_at

    def test_extracts_iso_timestamp(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Rate limit. Resumes at 2026-03-14T03:00:00",
        )
        assert result is not None
        assert "2026-03-14" in result.resume_at

    def test_no_resume_time(self, detector: RateLimitDetector):
        result = detector.detect(
            exit_code=1,
            error_message="Rate limit exceeded.",
        )
        assert result is not None
        assert result.resume_at == "unknown"


class TestYamlLoading:
    def test_load_from_real_config(self):
        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "autonomy.yaml"
        if not config_path.exists():
            pytest.skip("Config file not at expected path")
        detector = RateLimitDetector.from_yaml(config_path)
        # Should have loaded patterns from config
        result = detector.detect(
            exit_code=1, error_message="rate limit exceeded",
        )
        assert result is not None

    def test_load_missing_file(self, tmp_path: Path):
        detector = RateLimitDetector.from_yaml(tmp_path / "nonexistent.yaml")
        # Falls back to defaults
        result = detector.detect(
            exit_code=1, error_message="rate limit exceeded",
        )
        assert result is not None

    def test_load_custom_patterns(self, tmp_path: Path):
        config = tmp_path / "test.yaml"
        config.write_text(textwrap.dedent("""\
            rate_limit_patterns:
              exit_codes: [99]
              stderr_patterns:
                - "custom_limit"
        """))
        detector = RateLimitDetector.from_yaml(config)
        # Custom exit code
        assert detector.detect(exit_code=99, error_message="") is not None
        # Custom text pattern
        assert detector.detect(exit_code=1, error_message="custom_limit hit") is not None
        # Default patterns should NOT match
        assert detector.detect(exit_code=1, error_message="rate limit") is None


class TestRawMessagePreserved:
    def test_raw_message(self, detector: RateLimitDetector):
        msg = "Rate limit exceeded. Try again at 03:00 UTC."
        result = detector.detect(exit_code=1, error_message=msg)
        assert result is not None
        assert result.raw_message == msg
