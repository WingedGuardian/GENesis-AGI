"""The inbox URL coverage gate: did an evaluation account for every URL it was given?

Moved out of ``inbox/monitor.py`` unchanged (#2017). ``monitor`` imports back
the five names its call sites use; tests import from here directly. The names
stay package-private.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

# Patterns indicating the evaluation GAVE UP on URLs (not just encountered errors).
# Tested against all 8 existing response files: 0 false positives, 0 false negatives.
# Crucially, these do NOT include "ssl error" or "could not fetch" which appear
# in SUCCESSFUL evaluations that worked around a fetch failure.
_URL_FAILURE_PATTERNS = [
    "unfetchable",
    "unreachable from this host",
    "watch them yourself",
    "cannot evaluate the video",
    "cannot assess without content",
    "could not be fetched",
    "could not be accessed",
    "i could not fetch",
    "i could not access",
]


def _has_url_failures(response_text: str, input_content: str) -> bool:
    """Detect unresolved URL fetch failures in a CC evaluation response.

    Only triggers on definitive give-up language, not on error mentions
    that may appear in successful workaround descriptions.
    """
    urls = _extract_coverage_input_urls(input_content)
    if not urls:
        return False
    lower = response_text.lower()
    return any(p in lower for p in _URL_FAILURE_PATTERNS)


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

# A template placeholder, e.g. api.github.com/repos/{slug}. Requires a REAL
# {...} pair: a lone trailing brace picked up from surrounding prose
# ("see {https://example.com/secret-9f2}") must not exempt a live URL from
# the whole gate.
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")

# Coverage has a stricter grammar than general inbox discovery.  Both patterns
# preserve every non-whitespace terminal character (ambiguous punctuation must
# fail closed); the evidence side accepts only the evaluator's required Source
# field, optionally enclosed in RFC-style angle brackets.
#
# DISCOVERY and VALIDATION are deliberately separate patterns. They differ in
# exactly one place, because they are asked different questions.
_COVERAGE_URL_VALUE_RE = re.compile(
    r"(?:https?://[^\s<>]+)"
    r"|"
    r"(?:(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}/[^\s<>]+)",
    re.IGNORECASE,
)

# DISCOVERY scans free-form prose, so it must know where a token ENDS. Its
# bare-domain alternative therefore stops at `]`: without that, a markdown
# link's TEXT (`[example.com/a](https://example.com/a)`) matches here and then
# swallows `](https://...`, yielding one token spanning the label and the
# target -- an identity no response can ever cite. A scheme'd URL keeps `]` so
# an IPv6 authority (`https://[::1]:8443/p`) survives; a bare-domain form has
# no authority brackets to preserve.
#
# VALIDATION (`_COVERAGE_URL_VALUE_RE`, used as a fullmatch above) must NOT
# inherit that stop. Its input is a single already-delimited Source field, so
# there is no surrounding prose to end at, and narrowing it would silently
# reject a legitimate schemeless citation carrying a bracketed query parameter
# (`example.com/s?f[0]=x`) -- the URL would then read as uncovered even though
# the evaluator cited it exactly.
_COVERAGE_INPUT_URL_RE = re.compile(
    r"(?:https?://[^\s<>]+)"
    r"|"
    r"(?:(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}/[^\s<>\]]+)",
    re.IGNORECASE,
)
# A Source FIELD is a line whose only content before the label is Markdown
# container syntax: blockquote and list markers, nested in any order (#2020;
# `- > ` and `- - ` added in #2447 review). A bulleted multi-URL answer is the
# natural shape for "one Source per URL", and rejecting it read every
# correctly-cited URL as uncovered. The prefix is bounded to container syntax
# on purpose: the label after any word is prose, and must not become field
# evidence. Each alternative starts with a distinct character, so the repeat
# cannot backtrack super-linearly.
_SOURCE_FIELD_RE = re.compile(
    r"^[ \t]*(?:>[ \t]*|(?:[-*+]|\d{1,9}[.)])[ \t]+)*"
    r"\*\*Source:\*\*\s*(?P<source>\S(?:.*\S)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_coverage_input_urls(text: str) -> list[str]:
    """Discover input URLs without discarding identity-bearing characters."""
    return list(dict.fromkeys(match.group(0) for match in _COVERAGE_INPUT_URL_RE.finditer(text)))


# Prose punctuation that ends a sentence, and delimiters that come in pairs.
# The shared occurrence extractor uses source context to prove these wrappers
# before either prompt presentation or the gate can accept an alternate form.
_DISPLAY_TRIM_CHARS = ".,;:!?"
_DISPLAY_PAIRS = {")": "(", "]": "[", "}": "{", '"': '"', "'": "'", "`": "`"}


def _is_escaped(text: str, index: int) -> bool:
    """Whether an odd adjacent backslash run escapes this delimiter."""
    start = index
    while start and text[start - 1] == "\\":
        start -= 1
    return (index - start) % 2 == 1


def _display_url(url: str, *, wrappers: str = "") -> str:
    """Remove only matching wrappers observed immediately BEFORE this token.

    Without source context there is no proof a terminal delimiter is prose.
    Process outer to inner, with one counter and a moving end index; nested
    wrappers do not repeatedly copy or rescan the URL. A malformed/escaped
    wrapper leaves the original token intact. Punctuation inside the final
    wrapper remains part of the URL.
    """
    if not wrappers:
        return url
    counts = Counter(url)
    end = len(url)
    for opener in wrappers:
        while end and url[end - 1] in _DISPLAY_TRIM_CHARS:
            end -= 1
        closer = next(key for key, value in _DISPLAY_PAIRS.items() if value == opener)
        if not end or url[end - 1] != closer or _is_escaped(url, end - 1):
            return url
        remaining = counts[closer] - 1
        if (opener == closer and remaining % 2) or (
            opener != closer and counts[opener] > remaining
        ):
            return url
        counts[closer] -= 1
        end -= 1
    return url[:end]


@dataclass(frozen=True)
class _CoverageUrlOccurrence:
    raw: str
    start: int
    end: int
    display: str


def _coverage_url_occurrences(text: str) -> list[_CoverageUrlOccurrence]:
    """Keep every occurrence's own context, including repeated raw tokens."""
    occurrences = []
    openers = set(_DISPLAY_PAIRS.values())
    for match in _COVERAGE_INPUT_URL_RE.finditer(text):
        left = match.start()
        while left and text[left - 1] in openers and not _is_escaped(text, left - 1):
            left -= 1
        raw = match.group(0)
        occurrences.append(_CoverageUrlOccurrence(
            raw, match.start(), match.end(),
            _display_url(raw, wrappers=text[left:match.start()]),
        ))
    return occurrences


def _extract_source_urls(response_text: str) -> list[str]:
    """Parse lossless coverage evidence from required ``**Source:**`` fields."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in _SOURCE_FIELD_RE.finditer(response_text):
        value = match.group("source").strip()
        if value.startswith("<") and value.endswith(">"):
            value = value[1:-1]
        if _COVERAGE_URL_VALUE_RE.fullmatch(value) and value not in seen:
            seen.add(value)
            urls.append(value)
    return urls

def _coverage_identity(url: str) -> str | None:
    """Return the URL identity used by the citation-coverage gate.

    Scheme and a single leading ``www.`` label are presentation variants. Host
    case is insensitive. Everything else is identity-bearing: userinfo, port,
    path, query, and fragment are preserved exactly, apart from trailing slashes.
    Returning ``None`` keeps malformed authority-free URLs uncovered.
    """
    candidate = url if _SCHEME_RE.match(url) else f"//{url}"
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None

    hostname = hostname.lower().removeprefix("www.")
    raw_authority = parsed.netloc
    userinfo = raw_authority.rsplit("@", 1)[0] + "@" if "@" in raw_authority else ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = userinfo + rendered_host
    if port is not None:
        authority += f":{port}"

    identity = authority + parsed.path.rstrip("/")
    pre_fragment = url.split("#", 1)[0]
    if "?" in pre_fragment:
        identity += f"?{parsed.query}"
    if "#" in url:
        identity += f"#{parsed.fragment}"
    return identity


def _coverage_url_label(url: str) -> str:
    """Return a stable diagnostic id without copying URL credentials."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"url#{digest}"


def _uncovered_urls(response_text: str, input_content: str) -> list[str]:
    """Return input URLs not cited as complete parsed URL identities.

    Evidence comes only from Source fields. Every input occurrence keeps its
    source-proven presentation until after coverage is checked; repeated raw
    tokens in different contexts cannot erase each other's obligations.
    """
    return list(dict.fromkeys(
        occurrence.raw for occurrence in _uncovered_occurrences(
            response_text, _coverage_url_occurrences(input_content),
        )
    ))


def _uncovered_occurrences(
    response_text: str, occurrences: list[_CoverageUrlOccurrence],
) -> list[_CoverageUrlOccurrence]:
    """Shared live/report policy, without reconstructing or merging context."""
    response_identities = {
        identity
        for cited in _extract_source_urls(response_text)
        if (identity := _coverage_identity(cited)) is not None
    }
    return [
        occurrence for occurrence in occurrences
        if not _PLACEHOLDER_RE.search(occurrence.raw)
        and not _accepted_identities(occurrence.raw, display=occurrence.display)
        & response_identities
    ]


def _accepted_identities(url: str, *, display: str | None = None) -> set[str]:
    """Return the identities that count as citing THIS input URL.

    The alternate is supplied only by source-aware occurrence extraction.
    Without that context, only the raw identity counts. Never union these
    sets across repeated raw tokens: a literal and a wrapped occurrence can
    require different citations. An empty set leaves the occurrence uncovered.
    """
    return {
        identity
        for candidate in (url, display if display is not None else url)
        if (identity := _coverage_identity(candidate)) is not None
    }
