#!/usr/bin/env python3
"""Remedy-coverage semantics for a blocking gate's declared option set.

A gate that enumerates remedies in its block message is trusting the session to
relay them to the user faithfully. MEASURED 2026-08-31: the review escalation cap
printed three remedies and the relay dropped the first, invented a fourth, and
added "ship as-is" — the outcome that cap exists to prevent. The remedies are now
written to state as data (``review_state.write_gate_demand``); this module holds
the one question that makes them checkable:

    do these questions actually OFFER the declared remedies?

The split is storage vs meaning. ``review_state`` owns where a demand lives and
how long (branch-scoped counter state, git-aware). This module is stdlib-only and
does no I/O, so a PreToolUse hook can import it for the price of a parse.

FAIL DIRECTION: degrades toward "not covered" on any shape surprise. The payload
is harness-shaped and its schema is a CC-version artifact, so a drift must never
raise out of a hook — but it must never silently certify either.
"""

from __future__ import annotations

import re


def _label(option: object) -> str:
    """An option's LABEL only.

    Deliberately not the description. A description is prose about the option;
    the label is what the user picks. Matching on the description let an option
    named "Ship as-is" count as offering `shelve` because its prose said
    "neither shelve nor rework" — a mention, read as an offer.
    """
    return str(option.get("label", "")) if isinstance(option, dict) else ""


def _unmatched(question: object, keys: list[str]) -> list[str]:
    """Remedy keys with no option of their own — where the question's option set
    must BE the remedy set, one option each and nothing else.

    This is a bijection, not a count, and that distinction is the point. Three
    successive counting rules were defeated over the same lexical inputs, each
    narrower than the last:

      - "each remedy matches SOME option" — one option naming all three passed.
      - "each remedy matches a DISTINCT option" — the all-three option absorbed
        whichever remedy it was matched against, so adding the two options the
        refusal named passed.
      - "an option offers a remedy only if it names EXACTLY ONE" — closed both of
        those and still passed the acceptance case, because it only ever asked
        whether the remedies were PRESENT. An extra option is invisible to a
        presence test, and "ship as-is" alongside the three real remedies is
        precisely the corruption this gate was built for.

    The session writes the words, so no count over them can close the gap. What
    can be checked structurally is the SHAPE: this question offers these
    remedies and nothing else. An extra option means the menu is not the gate's
    menu, whatever it says.

    Other questions in the same call are untouched — this constrains ONE question,
    and the convention already expects several. A genuinely needed fourth option
    belongs in a second question, where it is the session's own, not presented as
    though the gate offered it.
    """
    if not isinstance(question, dict):
        return list(keys)
    options = question.get("options")
    if not isinstance(options, list):
        return list(keys)
    taken: set[int] = set()
    missing: list[str] = []
    for key in keys:
        for i, option in enumerate(options):
            if i not in taken and _matches_label(option, key):
                taken.add(i)
                break
        else:
            missing.append(key)
    if missing:
        return missing
    # Every remedy bound. Now the other half: nothing UNBOUND may remain, or the
    # user is being offered something the gate never sanctioned.
    if len(taken) != len(options):
        extra = [_label(o) for i, o in enumerate(options) if i not in taken]
        return [_EXTRA_PREFIX + ", ".join(x or "(unlabelled)" for x in extra)]
    return []


_EXTRA_PREFIX = "options this gate did not offer: "


def _matches_label(option: object, key: str) -> bool:
    pattern = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)", re.IGNORECASE)
    return bool(pattern.search(_label(option)))


def missing_remedies(questions: object, remedies: list[dict]) -> list[str]:
    """Remedy keys not offered by the single best-covering question.

    ONE question must carry the whole set. Spreading the remedies across separate
    questions presents them as independent choices rather than as the one decision
    the gate is asking for — the same corruption in a different shape — so partial
    coverage across questions does not add up.

    Other questions may ride alongside freely: the standing convention mandates at
    least two questions per call, so a clarifying question sharing the call must
    never make this refuse.

    Reported against the question covering the MOST, so a caller's message can name
    what to add rather than restating the entire set. Returns ``[]`` when some
    question covers everything, and when there is nothing to cover.
    """
    keys = [str(r.get("key", "")) for r in remedies if isinstance(r, dict) and r.get("key")]
    if not keys:
        return []
    if not isinstance(questions, list) or not questions:
        return list(keys)
    best: list[str] | None = None
    for question in questions:
        missing = _unmatched(question, keys)
        if not missing:
            return []
        if best is None or len(missing) < len(best):
            best = missing
    return best if best is not None else list(keys)
