"""The inbox URL coverage gate: did an evaluation account for every URL it was given?

Moved out of ``inbox/monitor.py`` unchanged (#2017). ``monitor`` imports back
the five names its call sites use; tests import from here directly. The names
stay package-private.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
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
_DISPLAY_CLOSERS = {opener: closer for closer, opener in _DISPLAY_PAIRS.items()}
_CONTEXT_DELIMITERS = re.compile(r"`+|[()\[\]{}'\"\r\n]")
_BACKTICK_RUNS = re.compile(r"`+")


def _is_escaped(text: str, index: int) -> bool:
    """Whether an odd adjacent backslash run escapes this delimiter."""
    start = index
    while start and text[start - 1] == "\\":
        start -= 1
    return (index - start) % 2 == 1


def _closing_wrapper_suffix(url: str, pos: int, wrappers: Sequence[str]) -> int | None:
    """After the FIRST source closure, accept only ordered closers/punctuation."""
    remaining = len(wrappers)
    while pos < len(url):
        if url[pos] in _DISPLAY_TRIM_CHARS:
            pos += 1
            continue
        if not remaining:
            return None
        opener = wrappers[remaining - 1]
        closer = _DISPLAY_CLOSERS.get(opener, opener)
        if opener.startswith("`"):
            run = _BACKTICK_RUNS.match(url, pos)
            if run is None or run.group() != opener:
                return None
        elif not url.startswith(closer, pos) or _is_escaped(url, pos):
            return None
        pos += len(closer)
        remaining -= 1
    return remaining


def _paired_source_closure(
    url: str, current: str, available: Counter[str],
) -> tuple[int | None, bool]:
    """Find a source closure, or report uncertainty from misnested URI pairs."""
    own: list[str] = []
    for match in _CONTEXT_DELIMITERS.finditer(url):
        delimiter = match.group()
        if _is_escaped(url, match.start()):
            continue
        if delimiter in ("(", "[", "{"):
            own.append(delimiter)
        elif delimiter in (")", "]", "}"):
            opener = _DISPLAY_PAIRS[delimiter]
            if own:
                if own.pop() != opener:
                    return None, True
            elif current == opener:
                return match.start(), False
            elif available[opener]:
                return None, True
        elif available[delimiter]:
            return None, True
    return None, False


def _symmetric_source_closure(url: str, current: str) -> int | None:
    """Quotes honor escapes; a code span closes at the first equal-width run."""
    for match in _CONTEXT_DELIMITERS.finditer(url):
        if match.group() != current:
            continue
        if current.startswith("`") or not _is_escaped(url, match.start()):
            return match.start()
    return None


def _url_wrapper_proof(
    url: str, wrappers: Sequence[str], wrapper_counts: Counter[str] | None = None,
) -> tuple[int, int | None]:
    """Return payload boundary and remaining source frames (None = uncertain).

    URL-internal openers never create source frames. The first source closure
    must start a wholly valid closing suffix; later payload/openers cannot
    restore that proof. Quote/code modes protect their other payload characters.
    """
    if not wrappers:
        return len(url), 0
    current = wrappers[-1]
    if current.startswith("`") or current in ("'", '"'):
        closure = _symmetric_source_closure(url, current)
    else:
        available = wrapper_counts if wrapper_counts is not None else Counter(wrappers)
        closure, uncertain = _paired_source_closure(url, current, available)
        if uncertain:
            return len(url), None
    if closure is None:
        return len(url), len(wrappers)
    remaining = _closing_wrapper_suffix(url, closure, wrappers)
    return (closure, remaining) if remaining is not None else (len(url), None)


def _display_url(
    url: str, *, wrappers: Sequence[str] = (), wrapper_counts: Counter[str] | None = None,
) -> str:
    """Return only the display identity justified by the shared source proof."""
    boundary, _ = _url_wrapper_proof(url, wrappers, wrapper_counts)
    return url[:boundary]


def _advance_wrapper_context(
    wrappers: list[str], counts: Counter[str], text: str, start: int, end: int,
) -> None:
    """Read only intervening prose and proved closing wrappers, never URL bytes."""
    for match in _CONTEXT_DELIMITERS.finditer(text, start, end):
        delimiter = match.group()
        if delimiter in ("\n", "\r"):
            wrappers.clear()
            counts.clear()
            continue
        in_code = bool(wrappers and wrappers[-1].startswith("`"))
        if in_code and delimiter != wrappers[-1]:
            continue
        # Backslashes are literal inside a code span, including before its
        # matching closing run (the installed MarkdownIt backtick rule).
        if not in_code and _is_escaped(text, match.start()):
            continue
        if (
            delimiter == "'" and 0 < match.start() < len(text) - 1
            and text[match.start() - 1].isalnum() and text[match.end()].isalnum()
        ):
            continue  # A contraction inside a word is not a prose quote.
        if wrappers and delimiter == wrappers[-1] and delimiter[0] in "'\"`":
            counts[wrappers.pop()] -= 1
        elif delimiter in ("'", '"') and counts[delimiter]:
            wrappers.clear()  # A source quote cannot close across an unclosed frame.
            counts.clear()
        elif delimiter in (")", "]", "}"):
            if wrappers and wrappers[-1] == _DISPLAY_PAIRS[delimiter]:
                counts[wrappers.pop()] -= 1
            else:
                wrappers.clear()
                counts.clear()
        else:
            wrappers.append(delimiter)
            counts[delimiter] += 1


@dataclass(frozen=True)
class _CoverageUrlOccurrence:
    raw: str
    start: int
    end: int
    display: str


def _coverage_url_occurrences(text: str) -> list[_CoverageUrlOccurrence]:
    """Keep every occurrence's own context, including repeated raw tokens."""
    occurrences = []
    wrappers: list[str] = []
    counts: Counter[str] = Counter()
    cursor = 0
    for match in _COVERAGE_INPUT_URL_RE.finditer(text):
        _advance_wrapper_context(wrappers, counts, text, cursor, match.start())
        raw = match.group(0)
        # The discovery regex excludes angle brackets: within an explicit
        # <URL> those bounds own the URL, not an outer prose delimiter. Inside
        # code, angle characters are payload and cannot shield code closure.
        angle_bounded = (
            match.start() > 0 and text[match.start() - 1] == "<"
            and not _is_escaped(text, match.start() - 1)
            and match.end() < len(text) and text[match.end()] == ">"
            and not (wrappers and wrappers[-1].startswith("`"))
        )
        boundary, remaining = (
            (len(raw), len(wrappers)) if angle_bounded
            else _url_wrapper_proof(raw, wrappers, counts)
        )
        display = raw[:boundary]
        occurrences.append(_CoverageUrlOccurrence(
            raw, match.start(), match.end(), display,
        ))
        if remaining is None:
            wrappers.clear()
            counts.clear()
        else:
            while len(wrappers) > remaining:
                counts[wrappers.pop()] -= 1
        cursor = match.end()
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
