"""Grounding score for built procedures — OBSERVABILITY, never a drop gate.

A procedure built from a session's action spine should quote the REAL
commands / paths / flags Genesis ran. ``grounding_score`` measures what
fraction of a procedure's distinctive step-tokens actually appear in the
*grounding haystack* (the uncapped record of executed tool inputs, from
``struggle_detector.build_spine_and_haystack``). A low score means the steps
quote commands that were never run — i.e. fabricated or merely discussed.

By design this is WARNING-ONLY (user decision 2026-06-30): the builder logs +
records a low score but ALWAYS stores the procedure. Every "drop" observed in
the C2b spikes was a FALSE NEGATIVE (a real procedure scored 0% only because
the earlier tokenizer treated a backtick-wrapped command as one atomic token),
and zero true hallucinations were seen. So grounding informs; it never controls.

Tokenizer (deterministically validated, ~/tmp/prc_grounding_proof.py): the 3
previously-dropped real procedures re-score 97-100%; fabricated/discussed-only
commands stay near 0%; templated procedures stay high after placeholder
normalization.
"""

from __future__ import annotations

import re

# Placeholders are normalized away before matching so a templated step
# (e.g. ``gh api repos/<owner>/<repo>``) still grounds against the concrete
# command Genesis ran (``gh api repos/WingedGuardian/Genesis``).
_PLACEHOLDER = re.compile(
    r"<[^>]+>|\$\{?\w+\}?|\bYYYY[-/]?MM[-/]?DD\b|\bv?\d+\.\dbXX\b|\bXX+\b"
)

# Distinctive command-ish tokens: long/short flags, paths (~ or /), and dotted
# identifiers (binaries.subcommands, file.ext, dotted ids). Extracted from the
# whole step text INCLUDING inside backtick spans (handled separately below).
_TECH = re.compile(r"--?[A-Za-z][\w-]{2,}|[/~][\w./\-]{3,}|\b\w[\w-]*\.[\w./-]{2,}")

# Split backtick-span contents on shell metacharacters + commas + quotes, so a
# wrapped command yields its individual binaries/flags/paths as tokens.
_BACKTICK_SPLIT = re.compile(r"[\s|&;<>()\"',]+")


def _step_tokens(step: str, *, normalize: bool = True) -> set[str]:
    """Distinctive command/path tokens from one step string."""
    s = _PLACEHOLDER.sub(" ", step) if normalize else step
    toks: set[str] = set(_TECH.findall(s))
    for span in re.findall(r"`([^`]+)`", s):
        for w in _BACKTICK_SPLIT.split(span):
            if len(w) >= 4 and re.search(r"[A-Za-z]", w) and not w.startswith("<"):
                toks.add(w)
    return toks


def grounding_score(steps: list[str], haystack: str) -> float:
    """Fraction of a procedure's distinctive step-tokens present in the haystack.

    Returns 1.0 (treated as "cannot assess — do not warn") when the haystack is
    empty OR the steps yield no distinctive tokens. Otherwise returns
    ``hits / total`` in [0, 1]. This function NEVER decides storage — callers
    log/record the value for observability only.
    """
    if not haystack:
        return 1.0
    toks: set[str] = set()
    for step in steps or []:
        if isinstance(step, str):
            toks |= _step_tokens(step)
    if not toks:
        return 1.0
    hits = sum(1 for t in toks if t in haystack)
    return hits / len(toks)
