"""Deterministic anti-slop detection + scrubbing for outbound content.

Ports the mechanically-checkable subset of voice-master's AI-tell audit (the
validated hand-built source at
``~/.genesis/output/skill-evolution/voice-master/antislop_scorer.py``) into the
repo, and adds *fix* logic. Two entry points:

- :func:`detect` — report AI-tell findings without mutating. Code regions
  (fenced blocks + inline code) are excluded so technical text is not
  false-flagged.
- :func:`scrub` — auto-FIX the one safe, unambiguous, fully-mechanical tell: a
  *spaced em dash* (``word — word``, the #1 AI punctuation tell) rewritten to a
  bare em dash (``word—word``). Everything else — banned words/phrases, cadence,
  and the *ambiguous* dashes (en dash, spaced ``--``, which also occur in number
  ranges, tables, and CLI flags) — is FLAGGED, never rewritten. Removing a word
  or guessing at an ambiguous dash would mangle meaning; real fixing needs
  regeneration.

This is the deterministic half of the anti-slop story: the model's own Step-6
self-audit is unreliable (it emitted spaced em dashes in every test despite the
SKILL.md's loudest rule), so the one mechanical tell is enforced in code on the
outbound path. ``scrub`` never silently degrades meaning — only the
zero-information-loss em-dash rewrite mutates the text.

CLI: ``echo "text" | python -m genesis.content.antislop -`` or
``python -m genesis.content.antislop path/to/file``.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

# --- Auto-fixable: a spaced EM dash (U+2014) flanked by horizontal whitespace.
#     Unambiguous (always a dash) and safe to collapse to a bare em dash.
#     Horizontal whitespace only — never merge across line breaks; a correctly
#     set em dash (no flanking spaces) is left alone. ---
_FIX_EMDASH = re.compile(r"[ \t]+—[ \t]+")

# --- Flag-only: ambiguous spaced dashes (en dash U+2013, or ``--``). These also
#     appear as number ranges ("5 – 10"), table rules ("| -- |"), and CLI
#     separators ("run -- foo"), so they are surfaced, never rewritten. ---
_FLAG_DASHES = re.compile(r"[ \t]+–[ \t]+|[ \t]+--[ \t]+")

# --- Code regions excluded from analysis + rewrite: fenced blocks and inline
#     code. Without this the rewrite/flags fire on code and identifiers. ---
_CODE_REGION = re.compile(r"```.*?```|`[^`]+`", re.DOTALL)

# --- Banned words (whole-word, case-insensitive). Faithful to the source list.
#     "clean" is intentionally omitted (allowed for literal cleanliness). ---
_BANNED_WORDS = [
    "delve", "leverage", "utilize", "ensure", "robust", "seamless", "streamline",
    "smoking gun", "landscape", "ecosystem", "holistic", "synergy", "empower",
    "elevate", "harness", "foster", "pivotal", "crucial", "enhance", "underscore",
    "vibrant", "testament", "showcase", "intricate", "evolving", "navigate",
    "journey", "cutting-edge", "game-changing", "transformative", "revolutionary",
]

_BANNED_PHRASES = [
    "it's worth noting", "it is worth noting", "it is important to note",
    "it's important to", "in conclusion", "in summary", "to summarize",
    "this allows us to", "this enables", "this ensures",
    "i'd be happy to", "i would be happy to",
]

# --- Sycophantic / filler openers (flagged only when they LEAD a sentence). ---
_FILLER_OPENERS = re.compile(
    r"(?:^|(?<=[.!?]\s))\s*(certainly|absolutely|of course|great question|"
    r"it's worth considering|it is worth considering)\b",
    re.IGNORECASE,
)

# --- Contrast cadence: "It's not X, it's Y" / "Not only A but B". ---
_CONTRAST_STRUCTURES = re.compile(
    r"\bit'?s not\b[^.?!]{1,60}?,?\s+it'?s\b"
    r"|\bnot just\b[^.?!]{1,60}?,?\s+but\b"
    r"|\bnot only\b[^.?!]{1,60}?,?\s+but\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScrubResult:
    """Outcome of :func:`scrub`.

    ``cleaned_text`` has the spaced-em-dash fix applied (and nothing else
    mutated). ``fixes_applied`` records mechanical rewrites actually made.
    ``flags`` are non-fixable tells surfaced for observability — populated only
    when ``is_voiced`` is True; never acted on automatically.
    """

    cleaned_text: str
    fixes_applied: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.fixes_applied and not self.flags


def _prose_only(text: str) -> str:
    """Return ``text`` with code regions blanked, for analysis."""
    return _CODE_REGION.sub(" ", text)


def _map_prose(text: str, fn) -> str:
    """Apply ``fn`` to prose spans only, leaving code regions byte-for-byte."""
    out: list[str] = []
    last = 0
    for m in _CODE_REGION.finditer(text):
        out.append(fn(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(text[last:]))
    return "".join(out)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


def detect(text: str) -> dict[str, object]:
    """Report AI-tell findings (no mutation). Code regions are excluded.

    Returns a dict of category -> evidence; empty dict means clean.
    """
    prose = _prose_only(text)
    findings: dict[str, object] = {}

    em = _FIX_EMDASH.findall(prose)
    if em:
        findings["spaced_em_dash"] = len(em)

    other = _FLAG_DASHES.findall(prose)
    if other:
        findings["spaced_ambiguous_dash"] = len(other)

    lower = prose.lower()
    banned_w = [w for w in _BANNED_WORDS if re.search(rf"\b{re.escape(w)}\b", lower)]
    if banned_w:
        findings["banned_words"] = banned_w

    banned_p = [p for p in _BANNED_PHRASES if p in lower]
    if banned_p:
        findings["banned_phrases"] = banned_p

    fillers = [m.group(1) for m in _FILLER_OPENERS.finditer(prose)]
    if fillers:
        findings["filler_openers"] = fillers

    contrasts = [m.group(0).strip() for m in _CONTRAST_STRUCTURES.finditer(prose)]
    if contrasts:
        findings["contrast_structures"] = contrasts

    sents = _sentences(prose)
    lengths = [len(s.split()) for s in sents]
    if len(lengths) > 3 and (max(lengths) - min(lengths) < 4):
        findings["uniform_sentence_length"] = len(lengths)

    return findings


def scrub(text: str, *, is_voiced: bool = True) -> ScrubResult:
    """Auto-fix the spaced em-dash tell; flag the rest (when ``is_voiced``).

    The em-dash rewrite is applied unconditionally (zero information loss, safe
    even for system text). ``flags`` — the non-fixable / ambiguous tells — are
    only populated for voiced content, so a templated alert containing "robust"
    isn't flagged.
    """
    fixes: list[str] = []
    em_count = 0

    def _fix_dashes(span: str) -> str:
        # Count during the per-span rewrite so fixes_applied always equals what
        # was actually changed (counting on _prose_only would over-report em
        # dashes that abut code-region boundaries and never get rewritten).
        nonlocal em_count
        new, n = _FIX_EMDASH.subn("—", span)
        em_count += n
        return new

    cleaned = _map_prose(text, _fix_dashes)
    if em_count:
        fixes.append(f"spaced_em_dash:{em_count}")

    flags: list[str] = []
    if is_voiced:
        findings = detect(cleaned)
        for key in (
            "spaced_ambiguous_dash", "banned_words", "banned_phrases",
            "filler_openers", "contrast_structures",
            "uniform_sentence_length",
        ):
            if key in findings:
                flags.append(f"{key}: {findings[key]}")

    return ScrubResult(cleaned_text=cleaned, fixes_applied=fixes, flags=flags)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    if src == "-":
        data = sys.stdin.read()
    else:
        with open(src, encoding="utf-8") as _fh:
            data = _fh.read()
    result = scrub(data)
    # Report to stderr; cleaned text to stdout so callers can pipe the result
    # (e.g. `python -m genesis.content.antislop draft.md > draft.clean.md`).
    print(f"fixes: {', '.join(result.fixes_applied) or 'none'}", file=sys.stderr)
    print(f"flags: {', '.join(result.flags) or 'none'}", file=sys.stderr)
    sys.stdout.write(result.cleaned_text)
