"""Content sanitizer — boundary markers and injection pattern detection.

Detection is LOG-ONLY. The sanitizer never blocks or modifies content.
It wraps content in boundary markers and annotates with risk scores so
downstream consumers can make informed decisions.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from genesis.security.patterns import InjectionPattern, load_default_patterns

logger = logging.getLogger(__name__)


class ContentSource(enum.Enum):
    """Origin of third-party content entering the system."""

    INBOX = "inbox"
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    MEMORY = "memory"
    RECON = "recon"
    EMAIL = "email"
    UNKNOWN = "unknown"


# Risk levels per source (higher = more dangerous)
_SOURCE_RISK: dict[ContentSource, float] = {
    ContentSource.INBOX: 0.8,  # Highest — raw files, skip_permissions CC
    ContentSource.WEB_FETCH: 0.6,  # Fetched web content
    ContentSource.WEB_SEARCH: 0.4,  # Search snippets
    ContentSource.RECON: 0.3,  # Recon findings
    ContentSource.EMAIL: 0.7,  # Email content — external, untrusted
    ContentSource.MEMORY: 0.2,  # Stored memories (already ingested)
    ContentSource.UNKNOWN: 0.5,
}


@dataclass(frozen=True)
class SanitizationResult:
    """Result of sanitizing content through the pipeline."""

    content: str  # Original content (unchanged)
    wrapped: str  # Content with boundary markers
    risk_score: float  # 0.0-1.0 (source_risk * max_pattern_severity)
    detected_patterns: list[str]  # Names of matched patterns
    source: ContentSource


class ContentSanitizer:
    """Sanitize third-party content before LLM prompt inclusion.

    Two capabilities:
    1. Boundary marker wrapping — wraps content in XML tags with source metadata
    2. Pattern detection — scans for injection patterns, returns risk score

    Detection is LOG-ONLY. Content is never blocked or modified.
    """

    def __init__(self, patterns: list[InjectionPattern] | None = None) -> None:
        self._patterns = patterns or load_default_patterns()

    @property
    def patterns(self) -> list[InjectionPattern]:
        """Return the current pattern list (read-only access)."""
        return list(self._patterns)

    def wrap_content(self, content: str, source: ContentSource) -> str:
        """Wrap content in boundary markers. Use this at ingestion points."""
        risk = _SOURCE_RISK.get(source, 0.5)
        return (
            f'<external-content source="{source.value}" risk="{risk:.1f}">\n'
            f"{content}\n"
            f"</external-content>"
        )

    def sanitize(self, content: str, source: ContentSource) -> SanitizationResult:
        """Full scan: wrap + detect patterns. Returns result with risk score.

        Risk score formula:
            risk = source_risk * (0.5 + max_severity * 0.5)

        - No patterns detected → risk = source_risk * 0.5
        - Max severity pattern (1.0) → risk = source_risk * 1.0
        - Score is always clamped to [0.0, 1.0]
        """
        wrapped = self.wrap_content(content, source)
        detected: list[str] = []
        max_severity = 0.0

        for pattern in self._patterns:
            if pattern.matches(content):
                detected.append(pattern.name)
                max_severity = max(max_severity, pattern.severity_score)

        source_risk = _SOURCE_RISK.get(source, 0.5)
        risk_score = min(1.0, source_risk * (0.5 + max_severity * 0.5))

        if detected:
            logger.info(
                "Injection patterns detected in %s content: %s (risk=%.3f)",
                source.value,
                detected,
                risk_score,
            )

        return SanitizationResult(
            content=content,
            wrapped=wrapped,
            risk_score=round(risk_score, 3),
            detected_patterns=detected,
            source=source,
        )
