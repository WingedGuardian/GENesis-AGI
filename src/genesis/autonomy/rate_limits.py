"""CC rate limit detection — parse CC CLI output for usage limit signals.

CC rate limit behavior is empirically observed, not a stable API.
Patterns are stored in config/autonomy.yaml for easy updates.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from genesis.autonomy.types import RateLimitEvent

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config" / "autonomy.yaml"

# Fallback patterns if config not loaded
_DEFAULT_STDERR_PATTERNS = [
    "rate limit",
    "usage limit",
    "Rate limit",
    "try again",
    "quota exceeded",
]

# Regex to extract "try again at HH:MM" or ISO timestamps from error messages
_RESUME_TIME_PATTERNS = [
    re.compile(r"try again (?:at|after) (\d{1,2}:\d{2}(?:\s*[AP]M)?(?:\s*UTC)?)", re.IGNORECASE),
    re.compile(r"resumes? (?:at|after) (\d{1,2}:\d{2}(?:\s*[AP]M)?(?:\s*UTC)?)", re.IGNORECASE),
    re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),  # ISO timestamp
]


class RateLimitDetector:
    """Detects CC rate limits from subprocess output.

    Uses configurable patterns from autonomy.yaml.
    Exit code detection is first priority (more stable); text patterns are fallback.
    """

    def __init__(
        self,
        *,
        exit_codes: list[int] | None = None,
        stderr_patterns: list[str] | None = None,
    ) -> None:
        self._exit_codes = set(exit_codes or [])
        self._patterns = stderr_patterns or list(_DEFAULT_STDERR_PATTERNS)

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> RateLimitDetector:
        """Load detection patterns from config."""
        config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
        try:
            data = yaml.safe_load(config_path.read_text())
            rl_config = data.get("rate_limit_patterns", {})
            return cls(
                exit_codes=rl_config.get("exit_codes", []),
                stderr_patterns=rl_config.get("stderr_patterns", list(_DEFAULT_STDERR_PATTERNS)),
            )
        except (yaml.YAMLError, OSError, AttributeError):
            logger.warning(
                "Failed to load rate limit config from %s — using defaults",
                config_path, exc_info=True,
            )
            return cls()

    def detect(self, *, exit_code: int, error_message: str) -> RateLimitEvent | None:
        """Check if a CC session failure was a rate limit.

        Returns RateLimitEvent if detected, None otherwise.
        """
        # Priority 1: known exit codes
        if self._exit_codes and exit_code in self._exit_codes:
            return self._build_event(error_message, source="exit_code")

        # Priority 2: text pattern matching in error message
        if not error_message:
            return None

        lowered = error_message.lower()
        for pattern in self._patterns:
            if pattern.lower() in lowered:
                return self._build_event(error_message, source="text_pattern")

        return None

    def _build_event(self, message: str, source: str) -> RateLimitEvent:
        """Construct a RateLimitEvent, extracting resume time if possible."""
        limit_type = self._classify_limit_type(message)
        resume_at = self._extract_resume_time(message)

        logger.warning(
            "CC rate limit detected (via %s): type=%s resume=%s",
            source, limit_type, resume_at or "unknown",
        )

        return RateLimitEvent(
            limit_type=limit_type,
            resume_at=resume_at or "unknown",
            raw_message=message,
        )

    @staticmethod
    def _classify_limit_type(message: str) -> str:
        """Best-effort classification of limit type from the message."""
        lowered = message.lower()
        if "monthly" in lowered or "month" in lowered:
            return "monthly"
        if "weekly" in lowered or "week" in lowered:
            return "weekly"
        if "daily" in lowered or "day" in lowered:
            return "daily"
        return "session"  # Default — most common

    @staticmethod
    def _extract_resume_time(message: str) -> str | None:
        """Extract resume time from the error message, if present."""
        for pattern in _RESUME_TIME_PATTERNS:
            match = pattern.search(message)
            if match:
                return match.group(1)
        return None
