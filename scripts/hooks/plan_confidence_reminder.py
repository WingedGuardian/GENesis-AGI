#!/usr/bin/env python3
"""PreToolUse/ExitPlanMode - fire the confidence reminder. Every time. Advisory.

WHAT THIS IS. One sentence the owner otherwise types by hand before every plan --
"give me your confidence and due diligence" -- emitted automatically at the
moment a plan is presented, so it becomes part of the process rather than
something they have to remember to say.

IT DOES NOT JUDGE THE PLAN, AND IT DOES NOT BLOCK. Both of those are corrections,
made three times, and they are written here so a later reader does not helpfully
restore either:

    "I'm not asking it to deny anything. This should not be a denying hook or a
     blocking hook. It's just an advisory hook to automatically fire that
     verbiage every time so that it's just an automatic part of that process."

    "We're not trying to do any detection over whether or not it's in the plan.
     Just fire the language every time. It doesn't matter if they've done it
     already or not."

WHAT THAT DELETED, and why the deletion IS the design. Earlier revisions read the
plan and stayed silent when they found a confidence figure. Every defect two
independent reviewers found in this hook lived in that reading, not in the hook:
a due-diligence VOCABULARY list that would have refused 21 of 203 real plans for
phrasing; a size cap inherited from a PR-body parser that silently judged the
largest 5% of plans on their first 64KB; a stray percentage anywhere in the text
satisfying the whole check. None of those failure modes is reachable now, because
nothing reads the plan. There is no detector to tune, no threshold to argue
about, and no false-positive/false-negative axis at all.

HOW THE BLOCK GOT IN, since that is the more instructive error. Asked what the
bar should be, the owner replied "but does it actually block?" -- a question
about whether blocking was POSSIBLE. It was read as a requirement that it SHOULD,
and a gate was built. The install's own standing hook axiom is that advisory is
the default and escalating to a block needs a specific, credible, MEASURED
reason. There was never one here: nothing about a plan lacking a confidence
figure is irreversible or destructive, which is the only thing that earns a
refusal.

So: `permissionDecision: "allow"`, and exit 0 on every path this module can
reach. The one exception is stated rather than swept up: an ImportError at MODULE
scope -- a missing sibling helper -- exits 1, because the `try/except` in
`__main__` cannot catch a failure that happens before it is installed. Under the
PreToolUse contract only exit 2 blocks, so exit 1 still costs nothing but the
reminder. "Always exit 0" was the wrong promise to write when a test in this
module's own suite proves the exception.

The one bound kept is the harness's own 10,000-character stdout cap, which is
externally imposed rather than invented: `print_json_bounded` trims the named
free-text field and never the envelope, so the decision survives a clip. Note
what that does NOT claim -- this reminder is a fixed string far under the budget,
so the writer is defence-in-depth here rather than a live bound. The call exists
so a later edit that grows the text cannot silently breach the cap, not because
anything currently approaches it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_input import read_payload  # noqa: E402
from hook_output import print_json_bounded  # noqa: E402

#: The verbiage. Names BOTH asks, because the reminder exists to replace a
#: sentence that always named both.
REMINDER = (
    "Before this plan goes to the user: state your CONFIDENCE and your DUE "
    "DILIGENCE.\n"
    "  - Confidence per item, as a percentage with the rationale, and what "
    "would change it — e.g. \"Item A: 85%; DISPROVEN if the probe shows the "
    "event does not fire.\"\n"
    "  - What you actually checked, and what you did NOT — e.g. \"MEASURED: 7 "
    "callers across 5 modules (Serena). NOT verified: whether the deploy path "
    "has run on this install.\"\n"
    "  - Anything below 90% gets investigated before it is planned around, not "
    "after.\n"
    "This fires on every plan. It is not a judgement about this one."
)


def main() -> int:
    payload = read_payload()

    # Scoping is intrinsic rather than inherited from the settings matcher, so a
    # broadened matcher cannot turn this into commentary on unrelated tools.
    # `None` is allowed so a hand-fed payload in a test is not silently a no-op.
    name = payload.get("tool_name") if isinstance(payload, dict) else None
    if name is not None and name != "ExitPlanMode":
        return 0

    print_json_bounded(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": REMINDER,
            }
        },
        text_keys=("hookSpecificOutput.additionalContext",),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # An advisory hook must never cost a tool call. There is no failure here
        # worth a non-zero exit: the worst case is a missing reminder.
        sys.exit(0)
