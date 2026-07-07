"""Canonical approve/reject vocabulary — the ONE place decision words live.

Three subsystems parse human approval language: the autonomous CLI approval
gate (quote-reply/voice text), the Telegram Approvals-topic bare-text path,
and the ego proposal reply parser. They historically kept three separate,
drifted word sets ("go" approved a quote-reply but not a bare message;
"sounds good" worked for proposals only). All consumers now import from
here; adding a word once adds it everywhere.

Two matching modes, deliberately distinct:

- TOKEN sets — single words, matched against a message's FIRST token
  ("Ok sounds good, ship it" → approved). Only safe in contexts already
  scoped to a decision (a quote-reply to an approval card, the Approvals
  topic, a voice reply to a prompt) — never run this over general chat.
- PHRASE sets — full-message matches for short standalone replies
  ("go for it", "ship it", a thumbs-up emoji). Safe in decision-scoped
  contexts; the whole (normalized) message must match.
"""

from __future__ import annotations

import re

APPROVE_TOKENS: frozenset[str] = frozenset({
    "approve", "approved", "ok", "okay", "yes", "go", "lgtm", "accept",
})
REJECT_TOKENS: frozenset[str] = frozenset({
    "reject", "rejected", "deny", "denied", "no", "nope", "skip",
})

APPROVE_PHRASES: frozenset[str] = frozenset({
    "ok", "okay", "yes", "yep", "yeah", "ya", "sure", "absolutely",
    "go for it", "do it", "let's go", "lets go", "go ahead",
    "proceed", "sounds good", "lgtm", "looks good",
    "approved", "approve", "accept",
    "ship it", "send it",
    "go", "alright", "aight",
    "\U0001f44d", "✅",
})
REJECT_PHRASES: frozenset[str] = frozenset({
    "no", "nope", "nah",
    "reject", "rejected", "deny", "denied",
    "skip", "pass",
    "don't", "dont", "not now", "hold off",
    "\U0001f44e", "❌",
})

_TRAILING_PUNCT_RE = re.compile(r"[\s.!,;:]+$")


def normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip trailing punctuation."""
    collapsed = " ".join((text or "").lower().split())
    return _TRAILING_PUNCT_RE.sub("", collapsed)


def phrase_decision(text: str) -> str | None:
    """'approved'/'rejected' iff the ENTIRE normalized message is a known
    standalone phrase. Conservative: anything longer returns None."""
    cleaned = normalize(text)
    if not cleaned:
        return None
    if cleaned in APPROVE_PHRASES:
        return "approved"
    if cleaned in REJECT_PHRASES:
        return "rejected"
    return None


def leading_token_decision(text: str) -> str | None:
    """'approved'/'rejected' from the FIRST token of the message.

    "Ok sounds good, and rename the flag" → approved. Only for contexts
    already scoped to a pending decision — the caller owns that scoping.
    """
    cleaned = normalize(text)
    if not cleaned:
        return None
    first = cleaned.split()[0].strip(".,:;!—-")
    if first in APPROVE_TOKENS:
        return "approved"
    if first in REJECT_TOKENS:
        return "rejected"
    return None


def scoped_decision(text: str) -> str | None:
    """Combined matcher for decision-scoped surfaces: exact phrase first
    (catches multi-word standalone replies), then leading token."""
    return phrase_decision(text) or leading_token_decision(text)
