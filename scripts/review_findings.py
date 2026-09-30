#!/usr/bin/env python3
"""Who reviews this repository, and how each reviewer's findings are read.

Two things live here, both stdlib-only so every gate can import them:

1. The REVIEWER TABLE, read from ``config/reviewers.yaml`` plus the install-local
   overlay ``~/.genesis/config/reviewers.local.yaml``. It names the reviewers
   whose reviews count, which one is primary (the one merge freshness asks for),
   which parser reads each one's findings, and the severity words that parser
   maps to ``floor`` / ``minor`` / ``analysis``. The merge gate reads from it the
   primary (merge freshness), the substitute set, and which logins its inline
   finding scanner reads with each parser; the round counter reads the primary.
   The severity WORDS are declared here but not yet consumed: each parser still
   maps its own vocabulary, which is the shipped one.
2. The per-reviewer SEVERITY PARSERS (Codex badges, CodeRabbit's header, Devin's
   marker), moved here from the merge gate so the round counter can read a
   finding with the same code the merge gate scores it with.

The config grammar is deliberately flat, because the hook tree has no YAML
dependency: one ``reviewers:`` block, one reviewer per indented line, the key its
REST login (with the ``[bot]`` suffix) and the value space-separated words::

    reviewers:
      chatgpt-codex-connector[bot]: primary parser=codex-badge floor=P1 minor=P2

Words are bare flags (``primary``, ``body-findings``, ``surface-only``,
``disabled``) or ``key=value`` pairs (``parser``, and the comma-separated word
lists ``floor``, ``minor``, ``analysis``). An overlay line for a login replaces
the shipped line; ``disabled`` removes the reviewer. Anything malformed makes the
whole table UNKNOWN (``None`` plus an error), never a partial table: a reviewer
silently dropped by a typo would un-count its reviews.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

#: The parsers this module registers, by the name config binds a reviewer to.
PARSERS = frozenset({"codex-badge", "coderabbit-header", "devin-marker", "codeql"})
_FLAGS = frozenset({"primary", "body-findings", "surface-only", "disabled"})
_LIST_KEYS = ("floor", "minor", "analysis")
# A GitHub APP's REST login: its lowercase slug plus the `[bot]` suffix. REQUIRED,
# not optional: a human account's login can never contain `[`, so the suffix is what
# proves a configured reviewer is an App. Without it an overlay could name the
# session's OWN account primary and satisfy merge freshness with its own review.
# Lowercase-only because App slugs are, and GitHub compares logins case-blind: a
# `CodeRabbitAI[bot]` line would otherwise match nothing and silently do nothing.
# Bounded and anchored, so a line cannot put markup into gate output.
_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}\[bot\]$")
_SHIPPED = Path(__file__).resolve().parents[1] / "config" / "reviewers.yaml"
_OVERLAY = Path.home() / ".genesis" / "config" / "reviewers.local.yaml"


@dataclass(frozen=True)
class Reviewer:
    """One configured reviewer. ``login`` is the REST login (``…[bot]``)."""

    login: str
    parser: str
    primary: bool = False
    body_findings: bool = False
    surface_only: bool = False
    floor: tuple[str, ...] = ()
    minor: tuple[str, ...] = ()
    analysis: tuple[str, ...] = ()


class _Malformed(ValueError):
    pass


def _strip_comment(line: str) -> str:
    if line.lstrip().startswith("#"):
        return ""
    # A comment needs whitespace before `#`, as in YAML, so a word such as
    # `floor=#1` could never be read as a comment. One linear pass: a regex such as
    # `\s+#` rescans a run of whitespace from every start position, which MEASURED
    # 85s on one 200,000-space line — and this runs on a hook path.
    for i in range(1, len(line)):
        if line[i] == "#" and line[i - 1].isspace():
            return line[:i].rstrip()
    return line.rstrip()


def _parse(text: str) -> dict[str, tuple[Reviewer | None, bool]]:
    """``{login: (reviewer or None when disabled, disabled)}`` for ONE file."""
    entries: dict[str, tuple[Reviewer | None, bool]] = {}
    in_block = False
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if not line[0].isspace():
            if line.strip() != "reviewers:" or in_block:
                raise _Malformed("only one top-level `reviewers:` block is allowed")
            in_block = True
            continue
        if not in_block:
            raise _Malformed("an indented line outside the `reviewers:` block")
        # partition, not a regex: this runs on a hook path, and a pattern with a lazy
        # group against a trailing `\s*$` backtracks quadratically on a long line.
        login, sep, rest = line.strip().partition(":")
        login, words = login.strip(), rest.split()
        if not sep or not login or " " in login:
            raise _Malformed("an entry is not `<login>: <words>`")
        if not _LOGIN_RE.fullmatch(login):
            raise _Malformed("a login is not a GitHub REST login")
        if login in entries:
            raise _Malformed("a login is listed twice in one file")
        flags: set[str] = set()
        values: dict[str, str] = {}
        for word in words:
            key, sep, value = word.partition("=")
            if not sep:
                if word not in _FLAGS or word in flags:
                    raise _Malformed("an unknown or repeated flag")
                flags.add(word)
            elif key not in ("parser", *_LIST_KEYS) or key in values or not value:
                raise _Malformed("an unknown, repeated or empty key")
            else:
                values[key] = value
        if "disabled" in flags:
            # `disabled` stands ALONE: a disabled line that also carries other words
            # is ambiguous about what was meant, so it is malformed, never a silent
            # removal of a reviewer.
            if len(flags) > 1 or values:
                raise _Malformed("`disabled` must stand alone")
            entries[login] = (None, True)
            continue
        parser = values.get("parser")
        if parser not in PARSERS:
            raise _Malformed("every reviewer needs a registered `parser=`")
        if (parser == "codeql") != ("surface-only" in flags):
            # CodeQL, and only CodeQL, is surface-only. Nothing scores its findings,
            # so a CodeQL reviewer that could stand in for the primary would be trusted
            # unenforced; and the scanner has no display-only mode for the scored
            # parsers, so `surface-only` on one would silently still score.
            raise _Malformed("surface-only goes with parser=codeql, and only there")
        lists = {key: tuple(w for w in values.get(key, "").split(",") if w) for key in _LIST_KEYS}
        entries[login] = (
            Reviewer(
                login=login,
                parser=parser,
                primary="primary" in flags,
                body_findings="body-findings" in flags,
                surface_only="surface-only" in flags,
                **lists,
            ),
            False,
        )
    return entries


def _source(env_name: str, path: Path) -> str | None:
    """The file's text, an env override's text, or None when absent."""
    override = os.environ.get(env_name)
    if override is not None:
        return override or None
    if not path.exists():
        return None
    # utf-8-sig: an editor that writes a byte-order mark must not make the whole
    # table malformed with an error that points at nothing visible.
    return path.read_text(encoding="utf-8-sig")


def configured_reviewers() -> tuple[dict[str, Reviewer] | None, str | None]:
    """``(table, None)`` or ``(None, error)``. Read fresh on every call.

    The table is ordered shipped-first, then overlay additions, in file order.
    Exactly one active reviewer is ``primary`` and it is not ``surface-only``;
    at least one active reviewer is not ``surface-only``. Anything else is an
    error, and every consumer treats an error as UNKNOWN. The env seams
    ``_TEST_REVIEWERS_YAML`` / ``_TEST_REVIEWERS_LOCAL_YAML`` replace the two
    files' TEXT (an empty overlay seam means "no overlay").
    """
    try:
        shipped = _source("_TEST_REVIEWERS_YAML", _SHIPPED)
        overlay = _source("_TEST_REVIEWERS_LOCAL_YAML", _OVERLAY)
    except (OSError, UnicodeDecodeError):
        return None, "reviewers_config_unreadable"
    if shipped is None:
        return None, "reviewers_config_missing"
    try:
        merged: dict[str, Reviewer | None] = {
            login: reviewer for login, (reviewer, _) in _parse(shipped).items()
        }
        if overlay is not None:
            for login, (reviewer, _) in _parse(overlay).items():
                merged[login] = reviewer
    except _Malformed:
        return None, "reviewers_config_malformed"
    table = {login: r for login, r in merged.items() if r is not None}
    primaries = [r for r in table.values() if r.primary]
    if len(primaries) != 1 or primaries[0].surface_only:
        return None, "reviewers_config_malformed"
    if not any(not r.surface_only for r in table.values()):
        return None, "reviewers_config_malformed"
    return table, None


def primary_reviewer_login() -> str | None:
    """The primary reviewer's REST login, or None when the table is unknown."""
    table, error = configured_reviewers()
    if error or not table:
        return None
    return next(login for login, r in table.items() if r.primary)


def enforced_logins() -> tuple[dict[str, frozenset[str]] | None, str | None]:
    """``({parser: logins}, None)`` — whose findings the merge gate READS with
    which parser — or ``(None, error)`` when the table is unknown.

    Every reviewer the table TRUSTS is here, so a trusted reviewer's findings are
    always read. And the SHIPPED file's reviewers are here even when the overlay
    disables one: the overlay lives outside the repo, in a file any session can
    write, so it may ADD enforcement but never remove it. ``disabled`` therefore
    withdraws a reviewer's trust (primary, stand-in) and leaves its findings scored.
    An unknown table is an error, never an empty set: an empty set would drop every
    finding to the unscored channel, which is a fail-open.
    """
    table, error = configured_reviewers()
    if error or not table:
        return None, error or "reviewers_config_missing"
    try:
        shipped = _parse(_source("_TEST_REVIEWERS_YAML", _SHIPPED) or "")
    except (_Malformed, OSError, UnicodeDecodeError):
        return None, "reviewers_config_malformed"
    by_parser: dict[str, set[str]] = {}
    for reviewer in [*table.values(), *(r for r, _ in shipped.values() if r is not None)]:
        by_parser.setdefault(reviewer.parser, set()).add(reviewer.login)
    return {parser: frozenset(logins) for parser, logins in by_parser.items()}, None


def substitute_reviewer_logins() -> tuple[str, ...]:
    """Every active non-primary reviewer that reviews (not ``surface-only``).

    Empty when the table is unknown: no substitute is ever offered on config the
    gate could not read.
    """
    table, error = configured_reviewers()
    if error or not table:
        return ()
    return tuple(login for login, r in table.items() if not r.primary and not r.surface_only)


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
