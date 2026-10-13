"""The inbox URL coverage gate: did an evaluation account for every URL it was given?

Moved out of ``inbox/monitor.py`` unchanged (#2017). ``monitor`` imports back
the five names its call sites use; tests import from here directly. The names
stay package-private.
"""

from __future__ import annotations

import hashlib
import re
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
# Used ONLY to render a coverage token for the prompt -- never to decide
# coverage identity.
_DISPLAY_TRIM_CHARS = ".,;:!?"
_DISPLAY_PAIRS = {")": "(", "]": "[", "}": "{", '"': '"', "'": "'", "`": "`"}


def _display_url(url: str) -> str:
    """Render a lossless coverage token as the URL the writer meant.

    The coverage grammar keeps every non-whitespace terminal character so the
    GATE can fail closed on ambiguous punctuation. That is wrong for the
    PROMPT: a markdown link or a quoted URL yields a token carrying its own
    wrapper, and telling the model to fetch ``https://example.com/foo)`` asks
    for a resource that does not exist.

    Trimming here is STRUCTURAL, not a prose heuristic. A paired delimiter is
    removed only when the remainder leaves it unmatched -- so a markdown
    wrapper goes and a balanced ``/wiki/Foo_(bar)`` stays, and an IPv6
    authority keeps its ``]`` because the ``[`` is still open. Sentence
    punctuation is trimmed outright.

    This runs on the presentation side only. ``_coverage_identity`` still
    compares the untrimmed token, so nothing here can make a truncated sibling
    vouch for an omitted URL.
    """
    candidate = url
    unwrapped = False
    while candidate:
        last = candidate[-1]
        if last in _DISPLAY_TRIM_CHARS:
            candidate = candidate[:-1]
            continue
        opener = _DISPLAY_PAIRS.get(last)
        if opener is None:
            break
        body = candidate[:-1]
        # Symmetric delimiters (quotes) pair off; asymmetric ones nest.
        unmatched = (
            body.count(last) % 2 == 0
            if opener == last
            else body.count(opener) <= body.count(last)
        )
        if not unmatched:
            break
        candidate = body
        unwrapped = True
    # Sentence punctuation alone is NOT evidence of a wrapper. `/path;` and
    # `/q?x=1!` are legal URLs, and round 3 established that such ambiguity
    # must fail CLOSED -- trimming them for display would ask the evaluator
    # for a DIFFERENT resource than the one the user saved, and the gate
    # would then accept that answer. So a trim only stands when it removed a
    # paired delimiter, which is structurally provable. Sentence punctuation
    # is consumed only to reach one (`...x",` -> `...x`).
    return candidate if unwrapped else url


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

    The response is first reduced to the URLs recognized by the canonical inbox
    scanner. Comparing parsed identities makes prefixes, sibling hosts, Unicode
    continuations, and legal URL delimiters different URLs by construction. It
    also avoids the prior substring matcher's growing boundary rules and keeps
    the scan linear in the number of extracted URLs.
    """
    urls = _extract_coverage_input_urls(input_content)
    if not urls:
        return []
    response_identities = {
        identity
        for cited in _extract_source_urls(response_text)
        if (identity := _coverage_identity(cited)) is not None
    }
    return [url for url in urls if not _PLACEHOLDER_RE.search(url)
            and not _accepted_identities(url) & response_identities]


def _accepted_identities(url: str) -> set[str]:
    """Return the identities that count as citing THIS input URL.

    Two renderings of one token, never a widened rule about URLs in general.
    The prompt shows ``_display_url(url)`` while the gate discovered ``url``,
    so a response that cites exactly what it was asked for must satisfy the
    gate -- otherwise the item can never be covered by any compliant answer.
    MEASURED 2026-09-14 over this install's corpus (284 stored baselines + 112
    live inbox files, 18,119 tokens): 331 (1.83%) render differently, and 72
    collapse two input tokens onto one prompt line. Without this, that 1.83%
    would be a permanent floor under the shadow flag rate -- and the shadow
    rate is precisely the signal the shadow->enforce decision is meant to read.

    Scoping is what keeps this safe. The set is derived from ONE token, so a
    truncated SIBLING still cannot vouch for it: given inputs ``/foo`` and
    ``/foo:bar``, neither one's display form is the other's identity. That is
    the guarantee the untrimmed comparison was introduced to provide, and it
    is unchanged.

    An empty set (both renderings unparseable) leaves the URL uncovered.
    """
    return {
        identity
        for candidate in (url, _display_url(url))
        if (identity := _coverage_identity(candidate)) is not None
    }
