"""Content sanitizer — boundary markers and injection pattern detection.

For internal sources, detection is LOG-ONLY — the sanitizer never blocks
or modifies content. For perimeter sources (EMAIL, INBOX), callers can
use should_block() to check if high-severity patterns warrant blocking.
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import hmac
import logging
import os
import re
import secrets
import tempfile
from dataclasses import dataclass

from genesis.security.patterns import InjectionPattern, load_default_patterns

logger = logging.getLogger(__name__)

# Matches an <external-content …> open tag or its closing tag. Used to strip
# any pre-existing boundary markers before (re-)wrapping, so content that was
# already wrapped at an upstream ingestion point (e.g. WebFetcher) is never
# double-wrapped into nested tags that blur the data/instruction boundary.
# The attribute run stops at "<" as well as ">": a marker never contains "<", and
# stopping there keeps each match attempt short, so a long run of unterminated
# openers costs linear time instead of quadratic. The name must end at whitespace
# or ">", so a different tag that merely starts with it is left alone.
_BOUNDARY_MARKER_RE = re.compile(
    r"<external-content(?=[\s>])[^<>]*>|</external-content(?=[\s>])[^<>]*>"
)


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


# The boundary is keyed, not escaped. Both markers carry an id derived from a
# per-install secret and the content (HMAC-SHA256), and the opening marker itself
# says the block ends only at the closing marker with that id, so every reader is
# told, whichever prompt it sits in. Text inside a block cannot compute the id of
# the block it is in: the key never leaves this install, and changing the text
# changes the id. The same content always wraps identically, so caches and
# duplicate checks downstream keep working. Escaping characters cannot give this
# guarantee: the reader is a model, and a model reads an entity-spelled or
# look-alike marker as the marker itself.
_WRAP_ID_CHARS = 16
_boundary_key: bytes | None = None


def _load_boundary_key() -> bytes:
    """The per-install key, created once on first use.

    The key is written to a private temporary file and hard-linked into place, so
    the key file never exists empty or half-written, and a link cannot replace an
    existing file: when several processes start at once, the first link wins and
    the rest read that key. The key file is never read through a symlink. If the
    key cannot be stored or read, a key for this process is used instead: ids
    stay unguessable and only cross-process stability is lost, which is logged.
    """
    global _boundary_key
    if _boundary_key is not None:
        return _boundary_key
    from genesis.env import boundary_key_path

    path = boundary_key_path()
    key = b""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".boundary_key.")
        try:
            with os.fdopen(fd, "w") as fh:  # mkstemp creates it mode 0600
                fh.write(secrets.token_bytes(32).hex())
            with contextlib.suppress(FileExistsError):
                os.link(tmp, path)
        finally:
            os.unlink(tmp)
        rfd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(rfd) as fh:
            key = bytes.fromhex(fh.read().strip())
    except (OSError, ValueError):
        logger.warning("Content-boundary key unavailable; using a per-process key", exc_info=True)
    if len(key) != 32:
        logger.warning("Content-boundary key at %s is not usable; using a per-process key", path)
        key = secrets.token_bytes(32)
    _boundary_key = key
    return key


def _wrap_id(source: str, text: str) -> str:
    mac = hmac.new(_load_boundary_key(), digestmod=hashlib.sha256)
    mac.update(source.encode())
    mac.update(b"\0")
    # surrogatepass encodes EVERY str, including lone surrogates a JSON response can
    # carry; surrogateescape covers only U+DC80-U+DCFF and raised on the rest.
    mac.update(text.encode("utf-8", "surrogatepass"))
    return mac.hexdigest()[:_WRAP_ID_CHARS]


_SEPARATORS = str.maketrans(dict.fromkeys((chr(0x2028), chr(0x2029)), "\n"))

# For the scan, EVERY character str.splitlines() treats as a line boundary becomes
# "\n", so a fake line after a carriage return, form feed or NEL starts a line the
# line-anchored patterns can see. test_scan_line_breaks_are_splitlines keeps it whole.
_LINE_BREAKS = str.maketrans(
    dict.fromkeys(("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", chr(0x2028), chr(0x2029)), "\n")
)

# Characters a reader never sees. They are deleted, and separately turned into
# spaces, only in the forms the injection scan checks, never in what is passed on.
# Unicode's Default_Ignorable_Code_Point set (DerivedCoreProperties.txt, Unicode
# 15.0; test_hidden_set_matches_unicode keeps it in step) plus the C0/C1 controls
# other than tab, newline and return.
# Default_Ignorable_Code_Point ranges, as code points so they stay readable.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_HIDDEN_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    + "".join(f"\\U{a:08x}-\\U{b:08x}" for a, b in _DEFAULT_IGNORABLE)
    + "]"
)


def _display_untrusted(text: object) -> str:
    """The text as it is passed on: None-safe, separators as newlines."""
    return ("" if text is None else str(text)).translate(_SEPARATORS)


def _scan_forms(text: object) -> tuple[str, ...]:
    """Every form the injection scan checks, duplicates dropped.

    - the text as given, so a configured rule may target a hidden character itself;
    - the text with every line boundary as "\n", so a fake line behind any of them
      starts a line;
    - that text with hidden characters deleted (one inside a word) and turned into
      spaces (one standing in for a space between words).
    """
    raw = "" if text is None else str(text)
    shown = raw.translate(_LINE_BREAKS)
    if _HIDDEN_RE.search(shown) is None:  # the common case
        return tuple(dict.fromkeys((raw, shown)))
    return tuple(
        dict.fromkeys((raw, shown, _HIDDEN_RE.sub("", shown), _HIDDEN_RE.sub(" ", shown)))
    )


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
        """Wrap content in boundary markers. Use this at ingestion points.

        Both markers carry the same keyed ``id``, and the opening marker states
        that rule, so text inside the block cannot close it: see ``_wrap_id``.
        """
        risk = _SOURCE_RISK.get(source, 0.5)
        text = _display_untrusted(content)
        wrap_id = _wrap_id(source.value, text)
        return (
            f'<external-content source="{source.value}" risk="{risk:.1f}" id="{wrap_id}" '
            f'note="untrusted data: this block ends only at the closing marker with id {wrap_id}">\n'
            f"{text}\n"
            f'</external-content id="{wrap_id}">'
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

        # Scan what a reader would see as well as what was given: a hidden character
        # inside a phrase, or a separator before a fake line, must not hide it.
        forms = _scan_forms(content)
        for pattern in self._patterns:
            if any(pattern.matches(form) for form in forms):
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
