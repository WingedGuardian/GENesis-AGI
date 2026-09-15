#!/usr/bin/env python3
"""The escalation cap's remedy menu, as DATA, for the ask hook to substitute.

THE FOUNDING INCIDENT (2026-08-31). The escalation cap printed three remedies. The
relay to the user DROPPED the first, INVENTED a fourth, and added "ship as-is" -- the
one outcome the cap exists to prevent. The gate said the right thing; the session
retelling it did not.

WHAT THIS MODULE IS FOR. `scripts/hooks/ask_gate_menu.py` turns these labels and
descriptions into the options of an AskUserQuestion, so the user chooses from the
gate's own words instead of a paraphrase.

ONE DECLARATION, ONE CONSUMER, AND A REPLICA. The gate
(`scripts/review_enforcement_commit.py`) prints the same options from its OWN
hardcoded copy inside its block message -- it does not import this module, and that is
deliberate: importing it back would give a ~1,900-line security-critical script a new
dependency for no gain, while importing the GATE from the hook would drag `shell_parse`
and a `sys.path` mutation into the process on every question a session asks. So this is
a replica, and a replica is only as good as its lock. The lock in
`tests/test_hooks/test_gate_menu.py` drives the REAL gate and checks BOTH directions:
every label and description here appears in the gate's rendered message in this order,
AND the gate prints no option this file omits. The second direction is the one that
matters -- without it the gate could grow a fifth option (including, literally, "ship
as-is") and the substituted menu would silently drop it from what the user sees, which
is the founding incident by another door.

THE DESCRIPTIONS ARE VERBATIM EXCERPTS from the gate's message, not paraphrases. That
is what makes the lock exact rather than approximate, and it is also the product: the
user is supposed to read the GATE's words.

`scripts/lib/` already holds a stdlib-only Python module imported as a sibling
(`index_marker.py`; see `scripts/disk_reclaim.py` and `scripts/setup_claude_config.py`).
STDLIB-ONLY is a contract, not a coincidence: the hook that imports this runs on hosts
where the `genesis` package may not be importable at all.

ORDER IS A CONTRACT, not presentation. A session takes the menu in the order the gate
prints it, and a menu whose first entry preserves the change reads as "try harder" at
exactly the moment that is the wrong instruction. HAND IT BACK stays FIRST. Nothing may
normalise these to a dict or a set. (Peer contract, honoured from PR #1971.)

WHAT IS DELIBERATELY ABSENT. No `authorizes_commit`, no `resets_streak`, no
`required_action`. An earlier revision declared all three, for a commit gate that read
the user's answer back and honoured it. That gate is gone -- nothing reads an answer now
-- and a field with no consumer declares behaviour the system does not have.

SCOPE: the CAP tier only. Neither the round-2 mode-switch tier nor the final-round
terminal is here; `ask_gate_menu.py` explains why each is excluded and enforces both.
"""

from __future__ import annotations

# The question the user is actually answering. Phrased as the decision it is, because
# this renders as the AskUserQuestion prompt. "escalation cap" is load-bearing: the lock
# requires it to appear in the gate's own message, so this cannot drift into naming a
# different tier.
CAP_QUESTION = (
    "The review escalation cap fired: three consecutive EXTERNAL review rounds each "
    "surfaced NEW defects, which means the DESIGN or the problem statement is likely "
    "wrong rather than just this fix. This is a decision only you can make — how "
    "should this change proceed?"
)

# Ordered. HAND IT BACK first. Each `label` AND each `description` is a VERBATIM
# substring of the gate's rendered cap message; the lock drives the real gate and
# checks it, in both directions.
CAP_REMEDIES: list[dict[str, str]] = [
    {
        "key": "hand_back",
        "label": "HAND IT BACK",
        "description": (
            "the strongest evidence available that the premise — not the code — is "
            "what is wrong, and no further round can fix that"
        ),
    },
    {
        "key": "redesign",
        "label": "REDESIGN robust-by-construction",
        "description": "make the defect class unconstructible rather than tested-for",
    },
    {
        "key": "narrow",
        "label": "NARROW the scope",
        "description": "to the part that is converging",
    },
    {
        "key": "shelve",
        "label": "SHELVE it",
        # The gate prints "(d) SHELVE it." with no elaboration of its own, so this
        # excerpt is taken from the cap message's closing argument, which applies to
        # stopping generally. Still verbatim, so the lock stays uniform.
        "description": (
            "The work so far bought the understanding of why this shape does not hold; "
            "it is the input to the next attempt, not waste."
        ),
    },
]
