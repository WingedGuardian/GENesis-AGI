"""Injection pattern definitions for content sanitization.

Patterns detect common prompt injection techniques. Detection is LOG-ONLY —
patterns never block content, only annotate risk scores for observability.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity constants
# ---------------------------------------------------------------------------
SEVERITY_HIGH = 0.9
SEVERITY_MEDIUM = 0.6
SEVERITY_LOW = 0.3


@dataclass(frozen=True)
class InjectionPattern:
    """A single injection detection pattern."""

    name: str
    regex: str
    severity: str  # LOW, MEDIUM, HIGH
    severity_score: float  # 0.0-1.0
    description: str
    _compiled: re.Pattern[str] = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    # Allow setting _compiled via __post_init__ on a frozen dataclass
    _SENTINEL: ClassVar[None] = None

    def __post_init__(self) -> None:
        compiled = re.compile(self.regex, re.MULTILINE)
        # Bypass frozen to set the compiled pattern
        object.__setattr__(self, "_compiled", compiled)

    def matches(self, content: str) -> bool:
        """Return True if this pattern matches anywhere in *content*."""
        return bool(self._compiled.search(content))


# ---------------------------------------------------------------------------
# Default hardcoded patterns
# ---------------------------------------------------------------------------
_DEFAULT_PATTERNS: list[InjectionPattern] = [
    # HIGH severity (0.9)
    InjectionPattern(
        name="role_override",
        regex=r"(?i)(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)",
        severity="HIGH",
        severity_score=SEVERITY_HIGH,
        description="Attempts to override prior instructions",
    ),
    InjectionPattern(
        name="identity_hijack",
        regex=r"(?i)you\s+are\s+(now|actually|really)\s+(a|an|the)\s+\w+",
        severity="HIGH",
        severity_score=SEVERITY_HIGH,
        description="Attempts to redefine the LLM identity",
    ),
    InjectionPattern(
        name="system_prompt_fake",
        regex=r"(?im)^(system|assistant)\s*:",
        severity="HIGH",
        severity_score=SEVERITY_HIGH,
        description="Fake system/assistant prompt delimiter",
    ),
    # MEDIUM severity (0.6)
    InjectionPattern(
        name="tool_invocation_fake",
        regex=r"(?i)</?tool_use>|</?function_call>|</?tool_result>",
        severity="MEDIUM",
        severity_score=SEVERITY_MEDIUM,
        description="Fake tool invocation XML tags",
    ),
    InjectionPattern(
        name="prompt_structure_fake",
        regex=r"(?im)^#{1,3}\s*(system|instructions|rules|constraints)\s*$",
        severity="MEDIUM",
        severity_score=SEVERITY_MEDIUM,
        description="Fake prompt structure headings",
    ),
    InjectionPattern(
        name="self_reference_manipulation",
        regex=r"(?i)(the\s+user|your\s+(creator|developer|instructions))\s+(wants|told|instructed|asked)\s+you\s+to",
        severity="MEDIUM",
        severity_score=SEVERITY_MEDIUM,
        description="Claims about user/developer intent to manipulate behavior",
    ),
    # LOW severity (0.3)
    InjectionPattern(
        name="authority_claim",
        regex=r"(?i)(as\s+)?(the\s+)?(admin|administrator|developer|owner|operator)\s*[,:]\s*(please\s+)?(do|execute|run|perform)",
        severity="LOW",
        severity_score=SEVERITY_LOW,
        description="Claims authority to issue commands",
    ),
    InjectionPattern(
        name="encoding_evasion",
        regex=r"(?i)(base64|rot13|hex)\s*(encode|decode|decrypt)",
        severity="LOW",
        severity_score=SEVERITY_LOW,
        description="References encoding schemes that may be evasion attempts",
    ),
]


def _load_patterns_from_yaml(path: Path) -> list[InjectionPattern] | None:
    """Attempt to load patterns from a YAML config file.

    Returns None if the file doesn't exist or can't be parsed.
    """
    if not path.is_file():
        return None

    try:
        import yaml  # noqa: PLC0415 — optional dependency at load time

        with open(path) as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict) or "patterns" not in data:
            logger.warning("YAML config at %s missing 'patterns' key", path)
            return None

        patterns: list[InjectionPattern] = []
        severity_scores = {"HIGH": SEVERITY_HIGH, "MEDIUM": SEVERITY_MEDIUM, "LOW": SEVERITY_LOW}

        for entry in data["patterns"]:
            sev = entry.get("severity", "MEDIUM").upper()
            patterns.append(
                InjectionPattern(
                    name=entry["name"],
                    regex=entry["regex"],
                    severity=sev,
                    severity_score=severity_scores.get(sev, SEVERITY_MEDIUM),
                    description=entry.get("description", ""),
                )
            )

        logger.info("Loaded %d injection patterns from %s", len(patterns), path)
        return patterns

    except Exception:
        logger.warning("Failed to load patterns from %s, using defaults", path, exc_info=True)
        return None


def load_default_patterns() -> list[InjectionPattern]:
    """Load injection patterns — YAML config if available, else hardcoded defaults."""
    # Look for config relative to the repo root
    config_candidates = [
        Path(__file__).resolve().parents[3] / "config" / "content_sanitization.yaml",
        Path("config/content_sanitization.yaml"),
    ]
    for candidate in config_candidates:
        result = _load_patterns_from_yaml(candidate)
        if result is not None:
            return result

    return list(_DEFAULT_PATTERNS)
