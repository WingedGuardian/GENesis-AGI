#!/usr/bin/env python3
"""PreToolUse/EnterPlanMode+ExitPlanMode - fire the confidence reminder. Advisory.

WHAT THIS IS. One sentence the owner otherwise types by hand before every plan --
"give me your confidence and due diligence" -- emitted automatically around plan
mode, so it becomes part of the process rather than something they have to
remember to say.

WHEN IT FIRES DECIDES WHAT IT CAN CHANGE, and the two moments are NOT equivalent.
An earlier revision of this hook was wired to ExitPlanMode alone and its docstring
claimed the reminder was "asked for BEFORE the plan is presented". That was false,
and a reviewer was right to say so: by the time a PreToolUse hook sees an
ExitPlanMode call, the model has already authored the plan, and `additionalContext`
reaches the model on its NEXT turn, while the plan goes to the user unchanged. So
on that path the reminder cannot touch the plan under review -- it reaches the
REVISION if the plan comes back, and the next plan in the session.

EnterPlanMode is the moment where it can. That call happens before any plan text
exists, so the reminder is in context while the plan is being written.

BOTH ARE WIRED, because EnterPlanMode alone covers a minority. MEASURED 2026-09-16,
counting `tool_use` blocks by name across this install's transcripts: the 40 most
recent give 10 EnterPlanMode against 109 ExitPlanMode; ALL 566 give 23 against 695,
and 39 of the 53 transcripts containing ExitPlanMode contain no EnterPlanMode at
all.

READ THAT AS A FLOOR, NOT A COVERAGE FIGURE, for two reasons that both came out of
review. The recent window runs about three times the corpus rate, so neither number
is stable enough to quote as "N% of plans are covered". And these are CALL counts:
one entry is commonly followed by a RUN of exits — 1 against 56 in a single session
here — so plans-per-entry is not measured by this at all. What it does establish,
firmly, is that ExitPlanMode must stay wired. WHY most sessions emit no entry call
is INFERRED (direct plan-mode entry by the user, which emits no tool call) rather
than measured, and closing that gap needs a trigger this hook does not have — not
a louder reminder.

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

So: NO `permissionDecision` AT ALL, and exit 0 on every path this module can
reach.

The missing field is deliberate and is the stronger form of "does not block".
Every other advisory hook here emits `permissionDecision: "allow"`, and an
earlier revision of this one copied that -- but `allow` is an active assertion
that the permission prompt should be SKIPPED, and `ExitPlanMode` is the one tool
in this repo's PreToolUse table whose entire purpose is to put a decision in
front of the user. Volunteering a verdict there is the last thing an advisory
reminder should do, and the correctness of doing it rested on undocumented
harness internals nothing pins across a CC bump.

READ from the CC bundle (2.1.246, `bin/claude.exe`), three mutually corroborating
places, which is what makes omission safe rather than merely tidier:
  - the schema declares `permissionDecision` OPTIONAL and `additionalContext` its
    SIBLING, not its child (@201331928);
  - the permission switch is GATED on the field's presence --
    `if(...hookEventName==="PreToolUse" && ...permissionDecision) switch(...)` --
    so omitting it leaves `permissionBehavior` unset (@212729553);
  - `additionalContext` is delivered on an INDEPENDENT branch,
    `if(et.additionalContext) yield {...}` (@212762710).
And `case "passthrough": case void 0: break` (@208567654) shows an absent
decision is the harness's own no-opinion state, not an error path. Multi-hook
precedence there is `deny > defer > ask > allow`, so an advisory `allow` could
never have overridden another hook anyway -- it could only ever have weakened
this one.

The one exception to exit 0 is stated rather than swept up: an ImportError at MODULE
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

#: One line per moment, because a reminder that misstates its own timing is the
#: defect this revision exists to fix. Neither line judges the plan.
MOMENT = {
    "EnterPlanMode": (
        "Entering plan mode. The plan you are about to write must carry the "
        "following — this arrives before you have written a line, so it is "
        "actionable now."
    ),
    "ExitPlanMode": (
        "This plan is already written, so this cannot change it. It applies to "
        "the revision if the plan comes back, and to the next plan in this "
        "session."
    ),
}

#: The tools this reminder attaches to — DERIVED from MOMENT rather than listed
#: beside it. Two hand-maintained lists is a distinction that has to be
#: remembered, and an audit found it already failing silently: a tool added to a
#: separate SCOPE tuple but forgotten in MOMENT fell through to the ExitPlanMode
#: wording, so an ENTRY moment would have been told "this plan is already
#: written" — the exact wrong-moment defect this revision exists to fix,
#: recreated one layer over, with the whole suite green. Derivation makes that
#: unconstructible instead of tested-for.
SCOPE = tuple(MOMENT)

#: The verbiage. Names BOTH asks, because the reminder exists to replace a
#: sentence that always named both.
REMINDER = (
    "State your CONFIDENCE and your DUE DILIGENCE.\n"
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
    if name is not None and name not in SCOPE:
        return 0

    # A hand-fed payload with no tool_name keeps the later moment's wording: it
    # is the conservative one, claiming less about what the reminder can reach.
    moment = MOMENT.get(name or "ExitPlanMode", MOMENT["ExitPlanMode"])

    print_json_bounded(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                # NO `permissionDecision`. See the docstring: the field is
                # optional, CC's permission switch is GATED on its presence, and
                # `additionalContext` is yielded on an independent branch — so
                # omitting it delivers the reminder and expresses no opinion on
                # whether the user is asked.
                "additionalContext": f"{moment}\n{REMINDER}",
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
