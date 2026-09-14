#!/usr/bin/env python3
"""Substitute the gate's own question into an ask.

THE PROBLEM THIS SOLVES. On 2026-08-31 the escalation cap printed three remedies.
The relay to the user dropped the first, invented a fourth, and added "ship as-is"
— the outcome the cap exists to prevent. The gate had said the right thing and the
agent relaying it had not.

WHY NOT VALIDATE THE AGENT'S OPTIONS. PR #1863 tried exactly that: compare the
agent's ``AskUserQuestion`` options against the declared remedy set and refuse a
mismatch. Four external review rounds, 16 findings, 7 P1, and the largest class
(4 findings, 3 of them P1) was the matching itself — a coverage rule over an OPEN
SET of words the agent chooses. Open sets do not converge; every named fix ships
the next round's gap. The premise was rejected.

WHAT THIS DOES INSTEAD. When the gate has recorded a menu, this hook APPENDS the
gate's own question — carrying the gate's own remedy labels — to whatever the agent
asked. The agent never authors those options, so it cannot drop, reword, negate or
pad them. The matching class does not get defended better; it stops existing.

AND THAT IS ALL IT DOES. The user's answer is not recorded and no gate reads one
back. An earlier revision of this work had a PostToolUse recorder and a commit gate
that honoured the recorded choice; four reviewers produced roughly twenty findings
against that half and zero against this one, and the record was forgeable in
principle by anything that can write the round file. So the authorisation path is
gone rather than defended, and every way this marker can be lost degrades to "the
menu does not appear this time" — the pre-change status quo, not a bypass.

MEASURED (CC 2.1.246, live in a real session, re-confirmed 2026-09-13):
  * ``updatedInput`` under ``hookSpecificOutput`` rewrites an ``AskUserQuestion``
    call: 2 questions were sent, 3 rendered, and the user answered the one this
    hook wrote.
  * It works ONLY without a ``permissionDecision`` field. Adding ``"allow"``
    breaks the call into "user did not answer" WITHOUT the user acting — a false
    negative that nearly killed the design. Never emit that field here; a test
    pins its absence.
  * Exactly ONE PreToolUse invocation per call, so an unconditional append cannot
    compound.
The docs carry no version contract for ``updatedInput``, so re-probe on a pin bump
(``docs/reference/cc-compatibility.md``).

WHAT IS DELIBERATELY ABSENT. The transcript. It records a tool call AS THE AGENT
EMITTED IT — substitution is applied afterwards — so a transcript-read
verification would read exactly the untrusted values it is trying to check.
#1863 did this; it must not come back.

FAIL DIRECTION — OPEN, on purpose, and against the house default. A guard that can
refuse a question could leave a session unable to ask the user ANYTHING, including
how to unwedge it. Its failure would cost more than its miss, so any internal error
here exits 0 silently and this hook is deliberately NOT wrapped in ``run_guard``.
Failing open is also cheap here in a way it usually is not: nothing downstream
authorises on this output, so a miss costs a menu rather than a gate.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# scripts/ (parent dir) for review_state — the same sibling-import idiom
# git_push_guard.py uses for the shared escalation-cap constants.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# AskUserQuestion's documented maximum. A call already at the limit is passed
# through UNMODIFIED rather than refused: refusing would reintroduce "the gate can
# wedge asks", the inverted fail direction #1863 defended. The demand simply stays
# live and the next ask gets the append.
_MAX_QUESTIONS = 4

# The OPTIONS axis has a hard MINIMUM as well as a maximum, and that asymmetry is
# why the count is bounded at all. MEASURED in the installed CC 2.1.246 binary:
# `options:Me(J7o()).min(2).max(4)`, alongside the steer CC returns when it is
# violated -- "This call included a question with fewer than 2 options, so it was
# rejected and the person never saw it ... Do not retry this call."
#
# So an out-of-range option count is NOT a shorter menu. It is a REJECTED CALL that
# takes the agent's own questions down with it and tells the agent not to retry --
# the session loses its ability to ask the user anything, which is the one outcome
# this hook's fail-direction note says it must never cause.
#
# THE COUNT IS BOUNDED AT THE READ, NOT HERE (`review_state._MIN_REMEDIES` /
# `_MAX_REMEDIES`), and this comment is the pointer rather than a second check. A
# duplicate guard here was written first and the verify-RED sweep found it VACUOUS:
# nothing can reach it, because `read_gate_demand` refuses the same counts one layer
# up, so disabling it left the test green. An unreachable guard is a convention
# wearing a lock's clothes. What stays below is the UNIQUENESS check, which IS
# reachable -- `_valid_remedy` validates entries one at a time and cannot see a
# collision between two of them.

# AskUserQuestion describes `header` as "Very short label ... (max 12 chars)" in
# CC 2.1.246. It is `z.string()` with no `.max()`, so an over-long value does not
# reject the call — it renders clipped. "Gate decision" is 13.
_HEADER = "Gate"

# THE KILL SWITCH IS NOT CHECKED HERE, and that is deliberate. It lives in
# `review_state.gate_ack_disabled`, where `read_gate_demand` consults it and reports
# "no demand" while it is armed — so this hook sees nothing to append and the commit
# gate takes its no-demand branch, which together IS the pre-demand behaviour.
#
# An earlier revision also checked it directly at both entry points here. That was
# redundant (the read below already short-circuits) and it made the switch's own test
# VACUOUS: a verify-RED mutation disabling the switch in review_state left the suite
# green, because this sibling layer still enforced it. One layer, one lock.


def _read_payload() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _payload_session(payload: dict) -> str | None:
    """The session this hook is serving, from the payload.

    This is the half of the association the gate cannot know and the hook can. The
    gate records WHICH session it blocked; this reports which session is asking. The
    two match exactly, or nothing happens — which is what replaced a global scan that
    could hand one session another session's decision.
    """
    sid = payload.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def _payload_cwd(payload: dict) -> str | None:
    """The SESSION's directory, from the payload — never this process's cwd.

    A demand is keyed per worktree, and the commit gate resolves the COMMIT's
    effective directory (`git -C <dir>`, the last `cd`, else the payload cwd). This
    hook used to resolve its own process cwd instead, and the two disagree in the
    exact configuration this repo mandates: a session working in a worktree while
    committing with `git -C <other-worktree>`.

    MEASURED when they disagree: the block writes the demand under one key, this
    hook reads another, nothing is ever appended, so the demand can never be
    answered and the branch can never commit. A permanent wedge whose block message
    promises a menu that will never render. Every other hook in the tree that needs
    the session directory reads it from the payload; this was the only one that did
    not.
    """
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _demand_for_ask(cwd: str | None = None, session_id: str | None = None) -> dict | None:
    """The demand this hook should act on, or None. Never raises."""
    try:
        from review_state import find_session_gate_demand
    except Exception:
        # review_state unimportable (partially-synced worktree, syntax error) —
        # degrade to doing nothing rather than to refusing an ask.
        return None
    try:
        # FIND the demand rather than computing its key. The commit gate keys it
        # under the COMMIT's effective directory, which `git -C <dir>` moves away
        # from the session's own — see find_session_gate_demand for the measurement.
        demand = find_session_gate_demand(cwd, session_id)
    except Exception:
        return None
    if not isinstance(demand, dict):
        return None
    # No state to filter on: a recorded menu is either readable, in which case the
    # user should see it, or it is not, in which case the reader already returned
    # None. An earlier revision gated on a LIVE/ANSWERED/CONSUMED state machine
    # because the commit gate read the answer back; nothing reads one now, so the
    # state machine went with it.
    return demand


def run_pre() -> int:
    payload = _read_payload()
    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return 0
    # NOTE the length check is AFTER the rewrite, not here. Checking up front was
    # both redundant and strictly worse: a call already at the maximum that CONTAINS
    # a forged gate question would have been waved through untouched, when replacing
    # it in place keeps the count identical and fixes the forgery. Only a genuine
    # APPEND can overflow.
    demand = _demand_for_ask(_payload_cwd(payload), _payload_session(payload))
    if not demand:
        return 0

    options = [
        {
            "label": remedy["label"],
            "description": remedy.get("description") or "",
        }
        for remedy in demand["remedies"]
    ]
    # Checked BEFORE building the canonical question, so a menu that cannot be
    # rendered never reaches the payload. Duplicates come only from a corrupt or
    # hand-edited marker, and the next block rewrites it from the canonical set.
    if len({o["label"] for o in options}) != len(options):
        return 0

    canonical = {
        "question": demand["question"],
        "header": _HEADER,
        "multiSelect": False,
        "options": options,
    }

    # REPLACE IN PLACE — never skip. An earlier revision returned early when the
    # gate's question was already present, on the assumption that the agent was
    # "faithfully echoing" one we had built. That assumption compared only the
    # QUESTION STRING and never the options, and it was the forgery path:
    #
    #   MEASURED — the agent reads the gate's question (the block message names
    #   `review_state.py gate-demand`, and it is a source literal besides), emits its
    #   own AskUserQuestion carrying that exact question with ONE option it wrote
    #   itself, the hook skipped, the recorder accepted the answer, and the acked
    #   commit exited 0. Three of four remedies dropped and the survivor reframed —
    #   the 2026-08-31 incident verbatim, laundered through the mechanism built to
    #   prevent it.
    #
    # Replacing is also strictly safe: substitution is idempotent, and the module
    # docstring's measurement (exactly ONE PreToolUse invocation per call) means
    # there was never a double-render to protect against in the first place.
    rewritten = [
        canonical if (isinstance(q, dict) and q.get("question") == demand["question"]) else q
        for q in questions
    ]
    if not any(q is canonical for q in rewritten):
        rewritten = [*rewritten, canonical]
    if len(rewritten) > _MAX_QUESTIONS:
        # Replacing cannot overflow, but appending can. Pass through rather than
        # refuse; the demand stays live for the next ask.
        return 0

    appended = dict(tool_input)
    appended["questions"] = rewritten
    # VARIANT B, and only variant B: hookSpecificOutput + updatedInput, with NO
    # permissionDecision field. See the module docstring's measurement.
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": appended,
                }
            }
        )
    )
    return 0


def main(argv: list[str]) -> int:
    # `--pre` is kept as an explicit mode rather than dropped for having one member:
    # the hook is wired by name in .claude/settings.json, and a hook invoked with a
    # mode it does not understand must do nothing rather than guess.
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "--pre":
        return run_pre()
    return 0


if __name__ == "__main__":
    # Deliberately NOT run_guard: see the module docstring's fail-direction note.
    # Any unexpected error exits 0 (non-blocking) rather than 2.
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
