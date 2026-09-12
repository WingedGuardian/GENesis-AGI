"""Content sanitizer — boundary markers and injection pattern detection.

For internal sources, detection is LOG-ONLY — the sanitizer never blocks
or modifies content. For perimeter sources (EMAIL, INBOX), callers can
use should_block() to check if high-severity patterns warrant blocking.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass

from genesis.security.patterns import InjectionPattern, load_default_patterns

logger = logging.getLogger(__name__)

# Matches an <external-content …> open tag or its closing tag. Used to strip
# any pre-existing boundary markers before (re-)wrapping, so content that was
# already wrapped at an upstream ingestion point (e.g. WebFetcher) is never
# double-wrapped into nested tags that blur the data/instruction boundary.
_BOUNDARY_MARKER_RE = re.compile(r"<external-content[^>]*>|</external-content>")


def strip_boundary_markers(text: str) -> str:
    """Remove any existing ``<external-content>`` boundary markers from text.

    Idempotent companion to :meth:`ContentSanitizer.wrap_content` — call this
    before wrapping content that may already carry markers from an upstream
    ingestion point, to avoid nested wrappers that confuse the LLM boundary.
    """
    return _BOUNDARY_MARKER_RE.sub("", text)


# Any maximal run of characters that break — or conceal a break in — a single line
# of text. Covers C0 (\x00-\x1f, incl. \t \n \r), C1 (\x7f-\x9f, incl. DEL/NEL), the
# Unicode line/paragraph separators (U+2028/U+2029) that Python's str.splitlines()
# also treats as line boundaries, and the zero-width / bidi format controls
# (ZWSP, LRM/RLM, the LRE..RLO and LRI..PDI overrides, BOM) that can visually reorder
# or hide injected text ("Trojan-source" concealment). Distinct from
# ContentSanitizer/wrap_content, which delimits a BLOCK of untrusted content; this
# normalizes a short SCALAR that flows verbatim into a line-parsed prompt.
# Which Cf (format) characters to strip is DERIVED from Python's Unicode database,
# not hand-picked — the previous hand-enumeration covered only 13 of Unicode's 170
# Cf codepoints, silently omitting concealment characters from the very families it
# did cover. ``test_cf_strip_set_matches_the_rule`` regenerates this set from
# ``unicodedata`` and fails if the two diverge, so a Python/UCD bump cannot quietly
# reopen the gap.
#
# THE RULE — strip a Cf codepoint iff it is INVISIBLE, i.e. one of:
#   * bidi class BN (boundary-neutral: the zero-width / ignorable family — ZWSP,
#     SOFT HYPHEN, WORD JOINER, the U+E0000 tag block, …),
#   * an explicit bidi override/isolate (LRE RLE LRO RLO PDF LRI RLI FSI PDI) —
#     the "Trojan source" reordering family,
#   * an invisible direction mark (LRM, RLM, ALM) — strong bidi class but
#     zero-width, so class alone does not catch them,
#   * an interlinear annotation control (U+FFF9-FFFB), which Unicode excludes from
#     plain-text interchange.
#
# Everything else in Cf is RENDERED script content and is kept. That matters: the
# Arabic number/ayah signs (U+0600-0605, U+06DD, U+08E2, U+0890-0891), SYRIAC
# ABBREVIATION MARK, the Kaithi number signs and the Egyptian hieroglyph joiners
# are all Cf, but they are visible marks in legitimate text — stripping them would
# corrupt the very content this function exists to pass through unharmed.
# U+200C ZWNJ and U+200D ZWJ are BN, and are the two deliberate exceptions: ZWJ
# builds every emoji ZWJ sequence and ZWNJ is orthographically required in Persian
# and Indic scripts.
#
# NOTE the resulting scope: no Cf character is a ``str.splitlines()`` boundary, so
# LINE FORGING is closed entirely by the C0/C1 + U+2028/U+2029 ranges below. The Cf
# set exists to close CONCEALMENT and visual REORDERING.
_CF_INVISIBLE = (
    r"\u00ad\u061c\u180e\u200b\u200e-\u200f\u202a-\u202e"
    r"\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb"
    r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0001"
    r"\U000e0020-\U000e007f"
)

_CONTROL_RUN_RE = re.compile(
    "["
    r"\x00-\x1f\x7f-\x9f"  # C0 + C1 control chars (incl. tab/newline/CR, NEL)
    r"\u2028\u2029"  # LINE / PARAGRAPH SEPARATOR (str.splitlines boundaries)
    + _CF_INVISIBLE  # the INVISIBLE Cf subset only (see the rule above)
    + "]+"
)


def strip_control_chars(s: str) -> str:
    """Collapse runs of line-breaking / line-concealing characters to a single space
    and trim the result — guaranteeing single-line, boundary-clean output.

    Signal free-text (``name``/``source``/``baseline_note``) is rendered one line per
    signal into a reflection/ego prompt that instructs the model "these are the ONLY
    signals you may cite"; a newline (or a Unicode line separator, or a bidi override)
    would forge or conceal an authoritative signal line. Applying this at the value's
    construction point closes the **line-forging** class (structural: no injected value
    can create a new prompt line) for every render path and enforces the one-line
    invariant.

    Scope note: this does NOT resist *semantic* injection via purely-printable text
    placed on a signal's own legitimate line (e.g. a crafted job name). That is
    defended at the input boundary — content-shape validation of the untrusted source
    (campaign-name validation), not here. A clean string is returned unchanged (modulo
    surrounding-whitespace trim).
    """
    return _CONTROL_RUN_RE.sub(" ", s).strip()


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


# Perimeter sources — inbound channels where an external actor can
# send content directly to Genesis. These get stricter treatment.
_PERIMETER_SOURCES = frozenset({ContentSource.EMAIL, ContentSource.INBOX})

# Risk threshold for perimeter blocking. HIGH severity (0.9) on EMAIL
# (source risk 0.7) gives: 0.7 * (0.5 + 0.9 * 0.5) = 0.665.
_PERIMETER_BLOCK_THRESHOLD = 0.6


class ContentSanitizer:
    """Sanitize third-party content before LLM prompt inclusion.

    Three capabilities:
    1. Boundary marker wrapping — wraps content in XML tags with source metadata
    2. Pattern detection — scans for injection patterns, returns risk score
    3. Perimeter blocking — should_block() for high-severity patterns on
       perimeter sources (EMAIL, INBOX). Internal paths remain log-only.
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

    @staticmethod
    def should_block(result: SanitizationResult) -> bool:
        """Check if content should be blocked at the perimeter.

        Only returns True for perimeter sources (EMAIL, INBOX) with
        high-severity injection patterns. Internal paths and low-risk
        patterns remain log-only and are never blocked.
        """
        if result.source not in _PERIMETER_SOURCES:
            return False
        return result.risk_score >= _PERIMETER_BLOCK_THRESHOLD
