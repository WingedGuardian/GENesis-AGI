#!/usr/bin/env python3
"""Who reviews this repository, and how each reviewer's findings are read.

Two things live here, both stdlib-only so every gate can import them:

1. The REVIEWER LIST, ``REVIEWERS``. It names the reviewers whose reviews count,
   which one is primary (the one merge freshness asks for), and which parser reads
   each one's findings. The merge gate reads from it the primary (merge
   freshness), the substitute set, and which logins its finding scanners read
   with each parser; the round counter reads the primary.
2. The per-reviewer SEVERITY PARSERS (Codex badges, CodeRabbit's header, Devin's
   marker), moved here from the merge gate so the round counter can read a
   finding with the same code the merge gate scores it with.

The list is code, not config, on purpose. It decides whose review satisfies the
merge gate, so it changes through a reviewed PR, exactly like the hook code that
enforces it. A config file would ADD write surfaces beyond the ones the hook code
already has (install overlays, the dashboard's config editor) and failure states
(missing, unreadable, malformed), for no benefit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: The parsers this module registers, by the name a reviewer is bound to.
PARSERS = frozenset({"codex-badge", "coderabbit-header", "devin-marker", "codeql"})
# A GitHub APP's REST login: its lowercase slug plus the `[bot]` suffix. REQUIRED,
# not optional: a human account's login can never contain `[`, so the suffix is what
# proves a listed reviewer is an App. Without it an entry could name the session's
# OWN account primary and satisfy merge freshness with its own review.
# Lowercase-only because App slugs are, and GitHub compares logins case-blind: a
# `CodeRabbitAI[bot]` entry would otherwise match nothing and silently do nothing.
# Bounded and anchored, so an entry cannot put markup into gate output.
_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}\[bot\]$")


@dataclass(frozen=True)
class Reviewer:
    """One reviewer. ``login`` is the REST login (``…[bot]``)."""

    login: str
    parser: str
    primary: bool = False
    surface_only: bool = False


# The review apps installed on this repository. `primary` is the reviewer merge
# freshness asks for (exactly one); `surface_only` findings are shown, never counted
# or scored.
REVIEWERS: tuple[Reviewer, ...] = (
    Reviewer(login="chatgpt-codex-connector[bot]", parser="codex-badge", primary=True),
    Reviewer(login="devin-ai-integration[bot]", parser="devin-marker"),
    Reviewer(login="coderabbitai[bot]", parser="coderabbit-header"),
    Reviewer(login="github-advanced-security[bot]", parser="codeql", surface_only=True),
)


def _validate(reviewers: tuple[Reviewer, ...]) -> None:
    """Raise ValueError when ``reviewers`` breaks an invariant.

    Run at import, so an edit that breaks one fails loudly there, and the merge
    gate's soft import turns that into a merge gate that blocks, naming the error.
    """
    primaries = [r for r in reviewers if r.primary]
    if len(primaries) != 1:
        raise ValueError("exactly one reviewer must be primary")
    if primaries[0].surface_only:
        raise ValueError("the primary reviewer cannot be surface-only")
    if primaries[0].parser != "codex-badge":
        # The round counter's clean-comment match, the `@codex review` request gate
        # and every freshness message are Codex-specific.
        raise ValueError("the primary must use the codex-badge parser")
    logins = [r.login for r in reviewers]
    if len(set(logins)) != len(logins):
        raise ValueError("a reviewer login is listed twice")
    for r in reviewers:
        if not _LOGIN_RE.fullmatch(r.login):
            raise ValueError(f"{r.login!r} is not a GitHub App REST login")
        if r.parser not in PARSERS:
            raise ValueError(f"{r.login}: unregistered parser {r.parser!r}")
        if (r.parser == "codeql") != r.surface_only:
            # CodeQL, and only CodeQL, is surface-only. Nothing scores its findings,
            # so a CodeQL reviewer that could stand in for the primary would be trusted
            # unenforced; and the scanner has no display-only mode for the scored
            # parsers, so `surface_only` on one would silently still score.
            raise ValueError(f"{r.login}: surface_only goes with parser 'codeql', and only there")
    if not any(not r.surface_only for r in reviewers):
        raise ValueError("at least one reviewer must not be surface-only")


_validate(REVIEWERS)


def primary_reviewer_login() -> str:
    """The primary reviewer's REST login: the one merge freshness asks for."""
    return next(r.login for r in REVIEWERS if r.primary)


def substitute_reviewer_logins() -> tuple[str, ...]:
    """Every non-primary reviewer that reviews (not ``surface_only``), in order."""
    return tuple(r.login for r in REVIEWERS if not r.primary and not r.surface_only)


def enforced_logins() -> dict[str, frozenset[str]]:
    """``{parser: logins}`` — whose findings the merge gate READS with which parser.

    Every reviewer the list names is here, so a trusted reviewer's findings are
    always read: trust and enforcement are bound to the same list.
    """
    by_parser: dict[str, set[str]] = {}
    for reviewer in REVIEWERS:
        by_parser.setdefault(reviewer.parser, set()).add(reviewer.login)
    return {parser: frozenset(logins) for parser, logins in by_parser.items()}


# ── Per-reviewer severity parsers (moved from the merge gate, unchanged) ──────
# Codex posts its actual P1/P2 findings ONLY as inline review comments, each
# opening with a severity badge.
INLINE_P1_RE = re.compile(r"!\[P1 Badge\]")
INLINE_P2_RE = re.compile(r"!\[P2 Badge\]")

# The documented ladder. An unrecognised level is NON-BLOCKING (surfaced with a
# canary) — a severity name this set has not seen must not silently start
# blocking every PR the moment the vendor adds one.
CR_SEVERITIES = frozenset({"critical", "major", "minor", "trivial", "info"})

# ONE header field: an italic span carrying no interior underscore. Anchored
# whole (`^…$`) so a field is recognised only as a complete span, never as a
# substring found somewhere inside one.
#
# The 64-char bound is deliberately double the observed ceiling, not tight to
# it. MEASURED across 124 real findings, the longest category field is
# `📐 Maintainability & Code Quality` at EXACTLY 32 characters — so a 32-char
# bound sits precisely on live data, and a vendor renaming one category one
# character longer would push a genuine Critical into the non-blocking path.
# The bound is a sanity check against runaway prose, not a filter doing real
# work, so it costs nothing to give it real headroom.
CR_HEADER_FIELD_RE = re.compile(r"^_([^_\n]{1,64})_$")
# A line that LOOKS like an attempted severity header — italic markers and a
# field separator — used only to tell "not a header" apart from "a header this
# code failed to parse". Conflating those two makes an unparsed finding print
# as "below Major", a false statement about a level that was never read.
CR_HEADER_SHAPE_RE = re.compile(r"^_.*\|.*_$")

DEVIN_META_PREFIX = "<!-- devin-review-comment "

DEVIN_MARKERS: dict[str, str] = {
    "🔴": "floor",  # severe bug
    "🟥": "floor",  # critical security
    "🟡": "minor",  # non-severe bug
    "🟨": "minor",  # security warning
    "🔍": "analysis",  # informational — asserts no defect
}


def cr_severity(body: str) -> tuple[str | None, bool]:
    """Severity read from a CodeRabbit finding's header LINE. -> (level, header_seen)

    Anchored to the header rather than searched for anywhere in the body, because
    a review-bot comment is a CODE-BEARING DOCUMENT: it quotes the diff and embeds
    ```suggestion``` blocks. `_` is simultaneously CodeRabbit's severity delimiter,
    markdown emphasis, AND the snake_case separator, so a body-wide search for a
    `_`-delimited severity word matches ordinary source. MEASURED against this
    repo, a whole-body search matched `MAX_CRITICAL_ERRORS`, `def is_major_bump`,
    the prose NEGATION `_not critical_`, the path `runtime_critical_path.py` —
    152 such tokens — and, self-demonstratingly, this feature's own test fixture
    `_CR_MAJOR_BODY`. A *Minor* finding quoting any one of them would have blocked
    the merge and been reported as "Critical/Major": worse than the blindness it
    replaces, since issue #1642 warns that poor precision trains reflex overrides.

    Every field must be a COMPLETE italic span, so a line is accepted as a header
    only if it is entirely one. Severity is read as the field's LAST word, never
    by position — one observed finding omits the severity field entirely, and a
    positional read would take the effort field ("Heavy lift") for a severity.
    The emoji is deliberately not matched: only Minor and Major were ever observed
    across 104 findings, so the emoji for Critical, Trivial and Info is unknown
    here and guessing it would silently miss the most severe level.

    `header_seen` separates "a CodeRabbit finding whose level we do not recognise"
    from "a comment with no severity header at all". Neither blocks; only the
    first is a canary worth printing. A line that LOOKS like a header but does
    not fully parse counts as SEEN — reporting it as "below Major" would be a
    false statement about a level this code never actually read.
    """
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        fields = [CR_HEADER_FIELD_RE.match(f.strip()) for f in stripped.split("|")]
        if not all(fields):
            # Header-SHAPED but unparseable is a canary, not a clean miss.
            return None, bool(CR_HEADER_SHAPE_RE.match(stripped))
        hits = []
        for fld in fields:
            words = fld.group(1).split()  # type: ignore[union-attr]
            if words and words[-1].casefold() in CR_SEVERITIES:
                hits.append(words[-1].casefold())
        if len(set(hits)) == 1 and hits:
            # Exactly one DISTINCT level — including the unanimous-duplicate
            # case (`_Business Critical_ | _🔴 Critical_`), where demoting a
            # real Critical to the non-blocking canary would be the worse read.
            return hits[0], True
        if len(hits) > 1:
            # Two severity-looking fields (`_Business Critical_ | _🟡 Minor_`)
            # is a format this code cannot adjudicate. First-match-wins read
            # that example as Critical — a false block; last-match-wins would
            # hide a real Major behind a decorative trailing field. Neither
            # guess is safe, so it is reported as unknown, the canary path
            # (Codex P2, PR #1677).
            return None, True
        return None, True  # a header, but no field names a level we know
    return None, False


def devin_finding(body: str) -> tuple[str | None, str | None]:
    """``(finding_id, severity_class)`` for a Devin inline comment.

    ``severity_class`` is a value of ``DEVIN_MARKERS`` ("floor" / "minor" /
    "analysis"), or None when the comment is not in the recognised shape — no
    leading metadata tag, unparseable metadata, or a marker outside the closed
    set. None is FORMAT DRIFT: the caller surfaces it and never scores it.

    ``finding_id`` is Devin's own id from the metadata, used to count a finding
    ONCE however many times it was posted (measured: Devin sometimes posts one
    finding twice, as two comments with one id). None when the metadata could
    not be read.

    Linear, no regex: this runs on a hook path whose deadline is a security
    boundary, over third-party text, so a scan that can backtrack is a
    fail-open waiting for a long enough body. `find`/`split` cannot.
    """
    text = body.lstrip()
    if not text.startswith(DEVIN_META_PREFIX):
        return None, None
    end = text.find("-->", len(DEVIN_META_PREFIX))
    if end < 0:
        return None, None
    finding_id: str | None = None
    try:
        meta = json.loads(text[len(DEVIN_META_PREFIX) : end])
    except Exception:
        meta = None
    if isinstance(meta, dict) and isinstance(meta.get("id"), str) and meta["id"]:
        finding_id = meta["id"]
    rest = text[end + 3 :].split(None, 1)
    marker = rest[0] if rest else ""
    if finding_id is None:
        # Unreadable metadata is drift even when the marker looks familiar: the
        # id is what dedup and reply-grouping rest on, and scoring a finding we
        # cannot identify would let one post count twice.
        return None, None
    return finding_id, DEVIN_MARKERS.get(marker)
