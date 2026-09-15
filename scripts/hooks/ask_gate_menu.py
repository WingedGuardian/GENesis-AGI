#!/usr/bin/env python3
"""Put the escalation cap's own menu in front of the user, not the session's retelling.

THE PROBLEM. On 2026-08-31 the escalation cap printed three remedies and the relay to
the user dropped the first, invented a fourth, and added "ship as-is" -- the one outcome
the cap exists to prevent. The gate had said the right thing; the agent relaying it had
not.

WHAT THIS DOES. When the review-round counter says the cap tier is live, this appends
the gate's OWN question -- carrying the gate's own remedy labels, in the gate's own
order -- to whatever the session asks next. The agent never authors those options, so it
cannot drop, reword, negate or pad them. The matching problem that killed two earlier
attempts (a coverage rule over an OPEN SET of agent-chosen words) stops existing rather
than getting defended better.

WHY THE COUNTER AND NOT A STORED MARKER. An earlier design persisted a "gate demand"
marker that the gate wrote on every block: question, remedies, branch, session. Across
four reviewers that marker and its lifecycle drew roughly 25 findings -- write, read,
retire, validation, scoping -- against ~3 for the substitution itself, at comparable
line counts. The last was a P1: the gate recorded `session_id: None` on every block
(it read the field from `tool_input` when it is a top-level key), so the cross-worktree
routing the marker existed for was inoperative in production and its own tests could not
see it. The marker only ever answered ONE question -- which tier is live -- and the
round counter already answers it, branch-scoped, with no lifecycle to get wrong. So the
marker is gone rather than patched again.

CAP TIER ONLY -- and "which tier is live" is TWO counters checked IN ORDER, not one.
The gate tests `lifetime >= FINAL_ROUND_CAP` FIRST, so above that line the live block is
normally the final-round terminal, whose options are ACCEPT-and-merge / ABANDON --
neither of which is in this menu. An earlier revision of this hook keyed on the streak
alone and therefore showed the CAP menu at the terminal: the user would have been handed
a menu OMITTING the only option that ends the loop and carrying three the live tier does
not offer. That is the founding incident arriving through the mechanism built to prevent
it, and the gate's own comment calls that state reachable. Both counters are read here,
in the gate's order.

A KNOWN GAP, stated because an earlier draft of this paragraph said "FIRST and returns"
and that is FALSE. The terminal returns only when the commit is NOT already carrying
`# final-round-accept`; when it is, the gate sets its spend flag and FALLS THROUGH to
the cap (`review_enforcement_commit.py:1027-1029`). So at streak >= cap AND lifetime >=
terminal, with that sigil present, the CAP block is what the user sees while this hook
stays silent -- uncovered, and it is exactly the state the gate's own comment names as
needing both sigils. It is not fixed here and the fix is not a better predicate: the
deciding input is a sigil on a future Bash commit command, which a PreToolUse hook on
AskUserQuestion structurally cannot see. The fail direction is the safe one (no menu,
session relays by hand = the pre-change behaviour), and `tests/test_hooks/
test_gate_menu.py` pins the gap so nobody "fixes" it into showing the cap menu at the
BARE terminal, which is the wrong-tier bug above.

The round-2 mode-switch tier is excluded for a different and equally mechanical reason:
`# audit-ack` deliberately does NOT reset the streak (a still-narrow fix must still
reach the round-3 stop), so a menu keyed on `round == 2` would follow the session
through every later question while it keeps working. The cap tier self-clears because
`# escalation-ack` calls `reset_review_round`.

HONEST LIMIT on that self-clearing, because the claim was once stated too broadly: it
holds for (b) REDESIGN, (c) NARROW and (d) SHELVE, which the user acknowledges. It does
NOT hold for (a) HAND IT BACK -- the gate's own message forbids the ack there, so the
streak stays at the cap and this menu re-appends to every later ask on the branch. That
is accepted rather than overlooked: the alternative is the retirement machinery this
design deleted, and a menu still offering "hand it back" to a session in the middle of
handing it back is inert noise, not a wrong instruction. The kill switch clears it if it
gets in the way.

FAIL DIRECTION -- OPEN, on purpose, and against the house default. A hook that can
refuse a question could leave a session unable to ask the user ANYTHING, including how
to unwedge it: its failure would cost more than its miss. So any internal error exits 0
silently and this is deliberately NOT wrapped in ``run_guard``. Failing open is also
unusually cheap here, because nothing downstream authorises on this hook's output -- a
miss costs a menu, never a gate. Every degradation reached through Python (unreadable
counter, missing module, version skew, kill switch, git hanging past the hook's timeout)
lands on "no menu this time", which is precisely the pre-change behaviour.

ONE DEGRADATION IS NOT IN THAT SET, and the guards below are load-bearing because of it.
MEASURED in the CC 2.1.246 binary: an `updatedInput` that fails the tool's input schema
is not ignored -- it is converted to a `deny` naming this hook, i.e. the "a hook can
wedge asks" failure this module vows never to cause. It is unreachable today (the model's
own input is schema-validated before hooks run, and `_canonical_question`'s bounds and
uniqueness checks plus the `_MAX_QUESTIONS` pass-through close the rest), which is the
only reason "never a gate" is true at all. So those checks are not belt-and-braces
against a retry: relaxing one on the grounds that "a miss costs a menu" would be wrong by
a whole severity level.

MEASURED (CC 2.1.246, live in a real session) and DOCUMENTED (Claude Code hooks
reference):
  * ``updatedInput`` under ``hookSpecificOutput`` on PreToolUse rewrites a tool's
    arguments before it runs, and is documented as applying to ``AskUserQuestion``.
  * It works ONLY without a ``permissionDecision`` field. MEASURED: adding ``"allow"``
    breaks the call into "user did not answer" WITHOUT the user acting -- a false
    negative that nearly killed the design. The docs independently note that a ``deny``
    alongside ``updatedInput`` discards the rewrite. Two reasons, one rule: never emit
    that field here. A test pins its absence.
  * Exactly ONE PreToolUse invocation per call, so an unconditional append cannot
    compound.
  * ``AskUserQuestion`` caps a call at 4 questions, and a question's options at 2..4
    (read from the binary: ``options:Me(J7o()).min(2).max(4)``). Out of range is not a
    shorter menu -- CC rejects the WHOLE call, the person never sees it, and the model
    is steered not to retry. That is the "a hook can wedge asks" failure this must never
    cause, which is why an over-long call is passed through untouched.
The docs carry no version contract for ``updatedInput`` and do not enumerate contexts
where it degrades, so re-probe on a pin bump (``docs/reference/cc-compatibility.md``).

DELIBERATELY ABSENT: the transcript. It records a tool call AS THE AGENT EMITTED IT --
substitution is applied afterwards -- so a transcript-read "verification" of what the
user was shown reads exactly the untrusted values it is trying to check.
"""

from __future__ import annotations

import json
import os
import sys

# Sibling imports, the established idiom (scripts/hooks/git_push_guard.py): this dir for
# hook_input, scripts/ for review_state, scripts/lib/ for the shared menu.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
for _p in (_HERE, _SCRIPTS, os.path.join(_SCRIPTS, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# AskUserQuestion's documented maximum. A call already at the limit is passed through
# UNMODIFIED rather than refused: refusing would reintroduce "the gate can wedge asks",
# the inverted fail direction this whole line of work exists to avoid.
_MAX_QUESTIONS = 4

# Measured from the CC 2.1.246 binary. A hard MINIMUM as well as a maximum, which the
# questions axis does not have -- an under-filled menu is as rejected as an over-full one.
_MIN_OPTIONS = 2
_MAX_OPTIONS = 4

# "Very short label displayed as a chip/tag (max 12 chars)" per the tool's own schema
# description. Not enforced by the schema, so an over-long value renders clipped rather
# than rejecting the call -- but there is no reason to ship a clipped chip.
_HEADER = "Gate"

_KILL_ENV = "GENESIS_GATE_MENU_DISABLED"
_KILL_MARKER = ".genesis/config/gate_menu_disabled"


def _disabled() -> bool:
    """True when the operator has turned the substitution off. Never raises.

    An env var for a one-off, a marker FILE for a durable disable -- a file whose mere
    existence is the signal has nothing to be malformed. Honest caveat, stated because
    it would otherwise read as insulation: anything the agent can write, it can write to
    disable itself. This is a RECOVERY tool for the operator, not a control on the agent.
    """
    # Case-folded, and "no"/"off" count as off. MEASURED before this: `=off` and `=no`
    # DISABLED the menu while `=false` did not and `=FALSE` did -- an operator who
    # writes a reasonable spelling got the opposite of what they meant.
    if os.environ.get(_KILL_ENV, "").strip().lower() not in ("", "0", "false", "no", "off"):
        return True
    try:
        from pathlib import Path

        return (Path.home() / _KILL_MARKER).exists()
    except Exception:  # noqa: BLE001 -- a kill-switch probe must never break an ask.
        return False


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _payload_cwd(payload: dict) -> str | None:
    """The directory the session is standing in, per the payload.

    Preferred over the process cwd because a hook's own cwd is not guaranteed to be the
    session's. Falls back to the process cwd, which is right in the ordinary case.

    KNOWN LIMIT, named rather than discovered later: the commit gate resolves the
    COMMIT's effective directory (`git -C <dir>` and a trailing `cd` both win over the
    session's own), so in the worktree-mandated `git -C <other worktree>` configuration
    this reads a DIFFERENT counter than the one that blocked. The result is no menu -- or,
    if the session's own worktree is itself at the cap, the correct menu for where the
    session is standing rather than for where it committed. Display-only either way, and
    the predecessor design's attempt to close this gap is exactly what never worked.
    """
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    try:
        return os.getcwd()
    except Exception:  # noqa: BLE001
        return None


def _cap_is_live(cwd: str | None) -> bool:
    """True when the escalation CAP tier is what the next commit would actually hit.

    MIRRORS THE GATE'S ORDER, which is the whole correctness argument. The gate reads
    two counters and the terminal wins:

        lifetime_n = get_review_lifetime(cwd)
        if lifetime_n >= FINAL_ROUND_CAP:   ... _deny(FINAL ROUND); return
        if round_n >= ESCALATION_ROUND_CAP: ... _deny(cap)

    So a menu keyed on the streak alone is WRONG above the terminal, not merely
    incomplete -- it would show cap options for a block that is offering
    ACCEPT-and-merge / ABANDON. Checking lifetime first is what keeps the menu and the
    block talking about the same tier.

    `get_review_round` and `get_review_lifetime` are both branch-scoped by construction
    -- each returns 0 when the stored state belongs to a different branch -- so a menu
    never follows you onto unrelated work.

    Every failure path returns False (no menu), including the one where the terminal
    cannot be read: if we cannot PROVE the terminal is clear, we say nothing rather than
    risk showing the wrong tier's options. Fail-open in the sense that matters -- a miss
    costs a menu, never a gate.
    """
    try:
        from review_state import (
            ESCALATION_ROUND_CAP,
            FINAL_ROUND_CAP,
            get_review_lifetime,
            get_review_round,
        )
    except Exception:  # noqa: BLE001 -- no counters, no menu. Never a refusal.
        return False
    try:
        if int(get_review_lifetime(cwd=cwd)) >= FINAL_ROUND_CAP:
            return False
        return int(get_review_round(cwd=cwd)) >= ESCALATION_ROUND_CAP
    except Exception:  # noqa: BLE001
        return False


def _canonical_question() -> dict | None:
    """The gate's own question, as an AskUserQuestion entry. None if unrenderable."""
    try:
        from gate_menu import CAP_QUESTION, CAP_REMEDIES
    except Exception:  # noqa: BLE001 -- version skew; degrade to no menu.
        return None
    try:
        options = [
            {"label": r["label"], "description": r.get("description") or ""} for r in CAP_REMEDIES
        ]
    except Exception:  # noqa: BLE001
        return None
    # Out of range is a REJECTED CALL, not a shorter menu -- it would take the agent's
    # own questions down with it. Unrenderable means we render nothing.
    if not _MIN_OPTIONS <= len(options) <= _MAX_OPTIONS:
        return None
    if len({o["label"] for o in options}) != len(options):
        return None
    return {
        "question": CAP_QUESTION,
        "header": _HEADER,
        "multiSelect": False,
        "options": options,
    }


def run() -> int:
    payload = _read_payload()
    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return 0
    # Every entry must already be a question-shaped dict. Such a call is schema-invalid
    # before this hook ever sees it, so rewriting it could only turn one broken call
    # into a differently broken one -- and emitting `updatedInput` over it would put
    # this hook's name on the failure.
    if not all(isinstance(q, dict) for q in questions):
        return 0
    if _disabled():
        return 0
    if not _cap_is_live(_payload_cwd(payload)):
        return 0

    canonical = _canonical_question()
    if canonical is None:
        return 0

    # REPLACE IN PLACE when the gate's question is already present -- never skip. An
    # earlier revision returned early on a match, on the assumption the agent was
    # faithfully echoing a question we had built. That assumption compared only the
    # QUESTION STRING and never the options, and it was the forgery path: MEASURED, an
    # agent could emit the gate's exact question carrying ONE option it wrote itself and
    # the hook would stand down -- the 2026-08-31 incident, laundered through the
    # mechanism built to prevent it. Replacing is idempotent and, with exactly one
    # PreToolUse invocation per call, there was never a double-render to protect against.
    rewritten = [
        canonical if (isinstance(q, dict) and q.get("question") == canonical["question"]) else q
        for q in questions
    ]
    if not any(q is canonical for q in rewritten):
        rewritten = [*rewritten, canonical]
    if len(rewritten) > _MAX_QUESTIONS:
        # Replacing cannot overflow; appending can. Pass through rather than refuse --
        # the counter is still at the cap, so the next ask gets the menu.
        return 0

    updated = dict(tool_input)
    updated["questions"] = rewritten
    # VARIANT B, and only variant B: hookSpecificOutput + updatedInput, with NO
    # permissionDecision field. See the module docstring for the two reasons.
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": updated,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    # Deliberately NOT run_guard: see the module docstring's fail-direction note. Any
    # unexpected error exits 0 (non-blocking) rather than 2.
    try:
        sys.exit(run())
    except Exception:  # noqa: BLE001
        sys.exit(0)
