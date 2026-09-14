#!/usr/bin/env python3
"""Substitute the gate's own question into an ask, and record the user's answer.

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

WHAT THIS DOES INSTEAD. While a demand is live, the PreToolUse half APPENDS the
gate's own question — carrying the gate's own remedy labels — to whatever the
agent asked. The agent never authors those options, so it cannot drop, reword,
negate or pad them. The matching class does not get defended better; it stops
existing. The PostToolUse half then reads the chosen label out of the harness's
own structured result and records it against the demand.

MEASURED (CC 2.1.246, live in a real session, re-confirmed 2026-09-13):
  * ``updatedInput`` under ``hookSpecificOutput`` rewrites an ``AskUserQuestion``
    call: 2 questions were sent, 3 rendered, and the user answered the one this
    hook wrote.
  * It works ONLY without a ``permissionDecision`` field. Adding ``"allow"``
    breaks the call into "user did not answer" WITHOUT the user acting — a false
    negative that nearly killed the design. Never emit that field here; a test
    pins its absence.
  * ``PostToolUse`` ``tool_response`` is ``{annotations, answers, questions}``,
    where ``answers`` maps the FULL question text to the chosen label (verified
    byte-exact, 0 characters lost at length 131). So the recorder needs no prose
    parsing and no transcript walk.
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
The enforcement that matters is fail-CLOSED and lives in the commit gate: with no
recorded answer, the commit stays blocked.
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

_HEADER = "Gate decision"

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


def _demand_for_ask(cwd: str | None = None) -> dict | None:
    """The demand this hook should act on, or None. Never raises."""
    try:
        from review_state import read_gate_demand
    except Exception:
        # review_state unimportable (partially-synced worktree, syntax error) —
        # degrade to doing nothing rather than to refusing an ask.
        return None
    try:
        demand = read_gate_demand(cwd)
    except Exception:
        return None
    if not isinstance(demand, dict):
        return None
    # LIVE: never answered. UNRECOGNISED: answered with something that mapped to no
    # declared remedy, so asking again is exactly right — the user gets the real
    # menu instead of another block telling them they were never asked.
    if demand.get("state") not in ("live", "unrecognised"):
        return None
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
    demand = _demand_for_ask(_payload_cwd(payload))
    if not demand:
        return 0

    options = [
        {
            "label": remedy["label"],
            "description": remedy.get("description") or "",
        }
        for remedy in demand["remedies"]
    ]
    if not options:
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


def run_post() -> int:
    payload = _read_payload()
    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    cwd = _payload_cwd(payload)
    demand = _demand_for_ask(cwd)
    if not demand:
        return 0
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return 0
    answers = response.get("answers")
    if not isinstance(answers, dict):
        return 0
    # Exact containment of the QUESTION the gate itself wrote. Both sides of the
    # match are gate-authored strings — a closed set, which is what makes this
    # matching sound where #1863's matching over agent-authored text was not.
    label = answers.get(demand["question"])
    if not isinstance(label, str):
        return 0
    try:
        from review_state import record_gate_answer

        record_gate_answer(question=demand["question"], label=label, cwd=cwd)
    except Exception:
        return 0
    return 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "--pre":
        return run_pre()
    if mode == "--post":
        return run_post()
    return 0


if __name__ == "__main__":
    # Deliberately NOT run_guard: see the module docstring's fail-direction note.
    # Any unexpected error exits 0 (non-blocking) rather than 2.
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
