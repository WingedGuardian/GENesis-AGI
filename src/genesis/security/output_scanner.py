"""Output content scanner — deterministic pattern matching for outbound content.

Scans outbound messages for sensitive data patterns before delivery.
High-confidence patterns only: API keys, specific IPs, credential
assignments, internal file paths. General technology mentions are
NOT flagged (those are handled by prompt-level guidance).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanResult:
    """Result of scanning outbound content."""

    safe: bool
    detected: list[str]
    risk_level: str  # "none", "medium", "high"


# High-confidence patterns that almost certainly indicate sensitive data
# leakage. Intentionally conservative to minimize false positives.
_OUTPUT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "api_key_openai",
        re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b"),
    ),
    (
        "api_key_anthropic",
        re.compile(r"\bsk-ant-[a-zA-Z0-9\-]{20,}\b"),
    ),
    (
        "api_key_groq",
        re.compile(r"\bgsk_[a-zA-Z0-9]{20,}\b"),
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)(password|token|secret|api[_\s]?key)\s*[:=]\s*['\"]?"
            r"[a-zA-Z0-9/+=_\-]{8,}",
        ),
    ),
    (
        "env_variable_secret",
        re.compile(r"\b[A-Z_]{3,}_(KEY|SECRET|TOKEN|PASSWORD)\s*=\s*\S+"),
    ),
    (
        "rfc1918_ip",
        re.compile(
            r"\b("
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r")\b"
        ),
    ),
    (
        "internal_file_path",
        re.compile(r"(?:/home/\w+/|~/\.genesis/|/etc/genesis)"),
    ),
    (
        "localhost_port",
        re.compile(r"\blocalhost:\d{4,5}\b"),
    ),
]

# Patterns that indicate critical leakage (immediate quarantine)
_CRITICAL_PATTERNS = frozenset({
    "api_key_openai",
    "api_key_anthropic",
    "api_key_groq",
    "credential_assignment",
    "env_variable_secret",
})


def scan_outbound(content: str) -> ScanResult:
    """Scan outbound content for sensitive data patterns.

    Returns a ScanResult indicating whether the content is safe to send.
    Only flags high-confidence patterns to minimize false positives.
    """
    detected: list[str] = []

    for name, pattern in _OUTPUT_PATTERNS:
        if pattern.search(content):
            detected.append(name)

    if not detected:
        return ScanResult(safe=True, detected=[], risk_level="none")

    has_critical = bool(_CRITICAL_PATTERNS & set(detected))
    risk_level = "high" if has_critical else "medium"

    logger.warning(
        "Outbound content scan: %s patterns detected (%s): %s",
        risk_level, len(detected), detected,
    )

    return ScanResult(safe=False, detected=detected, risk_level=risk_level)
