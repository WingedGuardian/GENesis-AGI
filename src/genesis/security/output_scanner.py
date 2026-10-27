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

from genesis.security.card_shapes import contains_card_number

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
        # Covers the whole modern sk-* family: legacy, sk-proj-, sk-svcacct-,
        # sk-admin- (OpenAI) and sk-or- (OpenRouter). The charset includes
        # '_' and '-' because current keys embed them; the shipped
        # [a-zA-Z0-9] charset silently missed every hyphen/underscore key
        # shape. Floor stays 20 (real keys are 40-165 chars) — a lower floor
        # false-positives on benign hyphenated slugs like "sk-learn-pipeline".
        "api_key_openai",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        # Anthropic key bodies contain underscores — the shipped charset
        # (no '_') could truncate a real key below the length floor.
        "api_key_anthropic",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "api_key_groq",
        re.compile(r"\bgsk_[a-zA-Z0-9]{20,}\b"),
    ),
    (
        # GitHub tokens, constrained to the real token shapes (high-confidence
        # contract): classic ghp_/gho_/ghu_/ghs_/ghr_ + 36 base62, and
        # fine-grained github_pat_ + 82. Prefixes are the exact {p,o,u,s,r}
        # set (not any gh[a-z]_) and lengths track the real bodies, so a
        # non-token like "ghi_abcdef…" cannot false-positive. The '_'
        # separator is GitHub's own anti-base64-collision design.
        "api_key_github",
        re.compile(r"\b(?:gh[posur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
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

# Patterns that indicate critical leakage (immediate quarantine).
# credential_assignment is intentionally NOT critical — it matches
# natural prose ("secret to our approach:", "token expiry is 86400s").
_CRITICAL_PATTERNS = frozenset(
    {
        "api_key_openai",
        "api_key_anthropic",
        "api_key_groq",
        "api_key_github",
        "env_variable_secret",
        "payment_card",
    }
)


def scan_outbound(content: str) -> ScanResult:
    """Scan outbound content for sensitive data patterns.

    Returns a ScanResult indicating whether the content is safe to send.
    Only flags high-confidence patterns to minimize false positives.
    """
    detected: list[str] = []

    for name, pattern in _OUTPUT_PATTERNS:
        if pattern.search(content):
            detected.append(name)
    # A card number needs a checksum, not just a regex, so it is checked here
    # rather than in the pattern table. Critical: it is never sent outward.
    if contains_card_number(content):
        detected.append("payment_card")

    if not detected:
        return ScanResult(safe=True, detected=[], risk_level="none")

    has_critical = bool(_CRITICAL_PATTERNS & set(detected))
    risk_level = "high" if has_critical else "medium"

    logger.warning(
        "Outbound content scan: %s patterns detected (%s): %s",
        risk_level,
        len(detected),
        detected,
    )

    return ScanResult(safe=False, detected=detected, risk_level=risk_level)


# Credential shapes a redactor can cut out in place. Location-style patterns
# (private IPs, home paths, localhost ports) are not secrets and are left as is;
# card numbers need a checksum, not a regex, so they are never redacted here.
_REDACTABLE = frozenset(
    {
        "api_key_openai",
        "api_key_anthropic",
        "api_key_groq",
        "api_key_github",
        "credential_assignment",
        "env_variable_secret",
    }
)
# The detector stops a credential_assignment value at its first character
# outside [A-Za-z0-9/+=_-]; redaction must take the whole token, or a password
# like "abcdefgh!rest" would keep "!rest". Same prefix, then everything up to a
# delimiter.
_REDACT_OVERRIDES: dict[str, re.Pattern[str]] = {
    "credential_assignment": re.compile(
        r"(?i)(password|token|secret|api[_\s]?key)\s*[:=]\s*['\"]?"
        r"[a-zA-Z0-9/+=_\-]{8,}[^\s'\"`,;)}\]]*",
    ),
}
_CREDENTIAL_KEY_WORDS = re.compile(r"(?i)password|token|secret|api[_\s]?key")
# A JSON member `"key": "value"`, matched without nested quantifiers so the scan
# stays linear on long quote-free text; the key is tested in the callback.
_JSON_MEMBER = re.compile(r'("[^"\n]{1,200}"\s*:\s*)"(?:[^"\\\n]|\\.)*"')


def _redact_json_member(match: re.Match[str]) -> str:
    if _CREDENTIAL_KEY_WORDS.search(match.group(1)):
        return match.group(1) + '"[redacted:json_credential]"'
    return match.group(0)


def redact_credentials(content: str) -> tuple[str, list[str]]:
    """Replace credential-shaped spans with ``[redacted:<pattern>]``.

    Returns the redacted text and the pattern names that matched. This is a
    best-effort cut for content headed to a trusted reader; callers that need a
    guarantee still run ``scan_outbound`` (or a stricter check) on the result.
    """
    found: list[str] = []
    for name, pattern in _OUTPUT_PATTERNS:
        if name not in _REDACTABLE:
            continue
        pattern = _REDACT_OVERRIDES.get(name, pattern)
        content, count = pattern.subn(f"[redacted:{name}]", content)
        if count:
            found.append(name)
    redacted = _JSON_MEMBER.sub(_redact_json_member, content)
    if redacted != content:
        found.append("json_credential")
    return redacted, found


def pattern_names() -> frozenset[str]:
    """Every name ``scan_outbound`` can report, for callers that allow-list some."""
    return frozenset(name for name, _ in _OUTPUT_PATTERNS) | {"payment_card"}
