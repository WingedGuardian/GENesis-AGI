"""Output content scanner — deterministic pattern matching for outbound content.

Scans outbound messages for sensitive data patterns before delivery.
High-confidence patterns only: API keys, specific IPs, credential
assignments, internal file paths. General technology mentions are
NOT flagged (those are handled by prompt-level guidance).
"""

from __future__ import annotations

import logging
import re
import unicodedata
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


# Location-style findings (private IPs, home paths, localhost ports): not secrets. A
# reader that keeps text for its owner may leave these in; anything else flagged is a
# reason to withhold.
LOCATION_FINDINGS = frozenset({"rfc1918_ip", "internal_file_path", "localhost_port"})


def _invisible_replaced(content: str, repl: str) -> str:
    return "".join(
        ch if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf") else repl
        for ch in content
    )


def scan_outbound_normalised(content: str) -> set[str]:
    """``scan_outbound`` findings on two normalisations of ``content``: Unicode
    control / format characters (categories Cc and Cf: NUL, zero-width, bidi) deleted,
    and replaced by a space. A credential such a character splits or hides from the
    pattern set is visible in one of them. Other invisible-looking characters (combining
    joiners, fillers) are not covered."""
    # Fast path: ``isprintable`` (C speed) is True only when no Cc/Cf character is
    # present (newline and tab are allowed by removing them first), and then both
    # normalisations equal ``content``: one scan answers it.
    if content.replace("\n", "").replace("\t", "").isprintable():
        return set(scan_outbound(content).detected)
    found: set[str] = set()
    for repl in ("", " "):
        found.update(scan_outbound(_invisible_replaced(content, repl)).detected)
    return found


def scan_diagnostic_controls(content: str) -> set[str]:
    """Findings before redaction for diagnostics with changing Cc/Cf controls.

    Ordinary text (including LF/TAB) needs no preflight. Keep the original view:
    deleting controls can erase a pattern boundary that the raw text preserves.
    Callers decide whether location-only findings are allowed in their output.
    """
    if _invisible_replaced(content, "") == content:
        return set()
    return set(scan_outbound(content).detected) | scan_outbound_normalised(content)


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
# An assignment the detectors flag is redacted from where the DETECTOR's match
# starts to the end of the line its match ends on. Trimming to the exact value
# meant parsing quotes, escapes and delimiters, and each parse left some tail
# ("abcdefgh!rest", a quoted "abc def") that no detector recognises any more.
# Taking the detector's own span, not a re-derived one, means a value it saw
# across a line break (CRLF, a blank line) goes too.
_ASSIGNMENTS = ("credential_assignment", "env_variable_secret")
# A JSON member is a credential when a WORD of its key names one (key split on
# punctuation and camelCase), unless the key's last word says the value is a
# number or flag ABOUT the credential (token_count, password_required).
# Free-form tails (name, id, url) do not exempt a key: their values could be
# the secret. "tokens" is deliberately absent: "max_tokens" is common.
_CREDENTIAL_WORDS = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "token",
        "apikey",
        "apikeys",
        "credential",
        "credentials",
        "passphrase",
        "authorization",
        "accesskey",
        "privatekey",
    }
)
_ABOUT_WORDS = frozenset(
    {
        "count",
        "type",
        "types",
        "required",
        "policy",
        "limit",
        "limits",
        "length",
        "size",
        "usage",
        "expiry",
        "expires",
        "expiration",
        "ttl",
    }
)
_KEY_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
# A JSON member `"key": "value"`, matched without nested quantifiers so the scan
# stays linear on long quote-free text; the key is judged in the callback.
_JSON_MEMBER = re.compile(r'"([^"\n]{1,200})"(\s*:\s*)"(?:[^"\\\n]|\\.)*"')


# One redactor per key shape, built once. It takes the whole token up to
# whitespace, a quote or a backslash, so a key followed by ".rest" or "!rest"
# cannot leave "rest" behind, and a key inside a JSON string keeps the string's
# closing quote.
_REDACTORS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern.pattern.removesuffix(r"\b") + r"""[^\s"'\\]*""")
    for name, pattern in _OUTPUT_PATTERNS
    if name in _REDACTABLE and name not in _ASSIGNMENTS
}
_DETECTORS: dict[str, re.Pattern[str]] = dict(_OUTPUT_PATTERNS)


def _is_credential_key(key: str) -> bool:
    words = [w.lower() for w in _KEY_WORD.findall(key)]
    joined = {a + b for a, b in zip(words, words[1:], strict=False)}  # "api"+"key", "private"+"key"
    named = _CREDENTIAL_WORDS & (set(words) | joined)
    return bool(named) and (not words or words[-1] not in _ABOUT_WORDS)


def _redact_json_member(match: re.Match[str]) -> str:
    if _is_credential_key(match.group(1)):
        return f'"{match.group(1)}"{match.group(2)}"[redacted:json_credential]"'
    return match.group(0)


def redact_credentials(content: str) -> tuple[str, list[str]]:
    """Replace credential-shaped spans with ``[redacted:<pattern>]``.

    Returns the redacted text and the pattern names that matched. This is a
    best-effort cut for content headed to a trusted reader; callers that need a
    guarantee still run ``scan_outbound`` (or a stricter check) on the result.
    """
    found: list[str] = []
    # JSON members first: a credential value is replaced whole, inside its quotes,
    # before a free-text redactor can reach it.
    redacted = _JSON_MEMBER.sub(_redact_json_member, content)
    if redacted != content:
        found.append("json_credential")
    content, cut = _cut_assignments(redacted)
    found += cut
    for name, pattern in _REDACTORS.items():
        content, count = pattern.subn(f"[redacted:{name}]", content)
        if count:
            found.append(name)
    return content, found


def _cut_assignments(content: str) -> tuple[str, list[str]]:
    """Cut each flagged assignment from its match to the end of its last line."""
    spans: list[tuple[int, int, str]] = []
    for name in _ASSIGNMENTS:
        newline = -1  # matches arrive in order: find each line's end once, not per match
        for match in _DETECTORS[name].finditer(content):
            if newline < match.end():
                newline = content.find("\n", match.end())
                newline = len(content) if newline < 0 else newline
            end = newline
            if end > match.start() and content[end - 1] == "\r":
                end -= 1  # keep a CRLF line ending whole
            spans.append((match.start(), max(end, match.end()), name))
    if not spans:
        return content, []
    spans.sort()
    merged = [list(spans[0])]
    for start, end, name in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, name])
    parts, pos = [], 0
    for start, end, name in merged:
        parts += [content[pos:start], f"[redacted:{name}]"]
        pos = end
    parts.append(content[pos:])
    return "".join(parts), sorted({name for _, _, name in spans}, key=_ASSIGNMENTS.index)


def pattern_names() -> frozenset[str]:
    """Every name ``scan_outbound`` can report, for callers that allow-list some."""
    return frozenset(name for name, _ in _OUTPUT_PATTERNS) | {"payment_card"}
