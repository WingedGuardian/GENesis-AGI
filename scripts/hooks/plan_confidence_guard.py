#!/usr/bin/env python3
"""PreToolUse/ExitPlanMode - the confidence reminder, delivered by the machine.

WHAT THIS IS, AND WHAT IT IS NOT. This is NOT a plan validator. It does not grade
a plan, score it, or judge whether its reasoning is any good. It automates ONE
thing the owner otherwise has to type by hand, over and over, before every plan:

    "give me your confidence and due diligence."

That reminder only helps if it lands BEFORE the plan is presented, which is why it
is a bounce rather than an advisory. A PreToolUse refusal returns the message to
the model with the plan unsent, and the harness carries a purpose-built path for
exactly this ("Please revise your plan based on the feedback and call ExitPlanMode
again"). So the refusal IS the delivery mechanism, not a punishment.

IT STAYS SILENT WHEN THE WORK IS ALREADY DONE. The trigger is the absence of a
confidence FIGURE (or an explicit reasoned opt-out) -- objective, and cheap to
satisfy. A plan that already states confidence never sees this hook speak.

WHY THE TRIGGER IS NOT "does the plan contain the word 'measured'". An earlier
revision blocked on a due-diligence VOCABULARY list as well, and that was the
validator design creeping back in: MEASURED 2026-09-07, 21 of 203 plans on this
install state a real confidence figure and would have been refused for phrasing --
"92% confident, I traced every caller and read chain.py end to end" contains none
of the eight words. Blocking that is lexical conformance wearing a gate's clothes,
and a false BLOCK is the strictly worse direction here (the same lesson
`_MIN_SUBSTANCE` in e2e_declaration.py records paying for). Diligence is still
ASKED for in the message, and still reported in the gap list; it is never on its
own a reason to bounce.

WHY THIS EXISTS AT ALL. CLAUDE.md's Confidence Framework already requires "explicit
confidence percentages with rationale" before planning work. The rule was carried
by prose alone and did not hold; the owner's summary was "you told me that was
already a thing, but it never fucking works." The mechanism that was meant to
enforce it, `scripts/plan_bookmark_hook.py`, fails for TWO independent reasons,
both READ from its source rather than assumed:

  1. WRONG MOMENT. It is wired `PostToolUse`, so it fires AFTER ExitPlanMode has
     already shown the plan to the user. Nothing it says can change what they just
     read.
  2. WRONG ARTIFACT, and the larger of the two. Its text (`_EXECUTION_PROTOCOL`)
     is an "EXECUTION PROTOCOL - before writing any code" whose step 2 is "STATE
     CONFIDENCE". That is guidance for IMPLEMENTING after approval. It never says
     the PLAN must carry confidence before it is presented. Aimed at the wrong
     thing even at the right moment.

WHY PreToolUse AND NOT PermissionRequest. Both fire for ExitPlanMode -- MEASURED
2026-09-07 with a live probe, because the published docs show only the
PermissionRequest form and are silent on this one:

    16:51:35.713  PreToolUse         ExitPlanMode  tool_input=['plan','planFilePath']
    16:51:36.046  PermissionRequest  ExitPlanMode  tool_input=['plan','planFilePath']

PreToolUse wins on every axis: it fires 333ms earlier, its payload also carries
`tool_use_id`, and it is the event with the documented exit-2 deny convention.
The probe also recorded `permission_mode == 'plan'` during the call even though
the session runs `--dangerously-skip-permissions` -- plan mode overrides the
session bypass, so neither event is starved by it.

FAIL OPEN, DELIBERATELY, and do NOT wrap this in `run_guard`. That helper converts
an unexpected crash into exit 2, and its own docstring restricts it to
irreversible-action guards: "never for advisory or convenience guards, which must
stay fail-open so a bug never blocks legit work." A bug here would refuse EVERY
plan in every session with no way to present the fix, so every unexpected path
returns 0. What this guard bounces is exactly one thing: a plan it could read that
states no confidence figure and offers no reasoned opt-out.

THE ESCAPE HATCH IS NOT OPTIONAL. A blocking gate with no sanctioned way to say
"not applicable" is a closed loop -- the author is refused, has nothing true to
write, and learns to route around the gate. `Confidence: none - <reason>` is that
answer, copied deliberately from the sibling `E2E: none - <reason>` convention
(`scripts/e2e_declaration.py`) so the repo has ONE dialect for "the decision was
made, and the answer is that it does not apply" rather than two.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import re
import sys

#: The scan window, and the guard REFUSES TO JUDGE beyond it rather than judging a
#: prefix. Truncating and then scanning FALSELY REFUSES a compliant plan whose
#: confidence figures sit past the cut, which is the one direction this guard must
#: never fail in.
#:
#: THE NUMBER IS THE SIBLING'S, NOT A CHOICE. `readable_plan` delegates to
#: `e2e_declaration.readable_body`, which truncates at its own `_MAX_BODY`
#: (e2e_declaration.py:86, applied at :241 and again in the local fallback at
#: :203). That is GitHub's PR-body cap -- correct for a PR body, never derived for
#: a plan. So the effective window is 65_536 whatever this constant says, and the
#: only honest value is the one that matches it.
#:
#: TWO REVISIONS GOT THIS WRONG, THE SECOND WHILE FIXING THE FIRST. Revision 1 set
#: 200_000 on the claim "the largest observed was ~42_000" -- one observation (this
#: session's own plan, via the hook probe) generalised to a corpus nobody had
#: counted; the true largest is 469_353. Revision 2 raised it to 1_000_000 and
#: added a fail-open branch, believing the prefix scan was gone -- but the
#: truncation had simply moved into the imported stripper, unread. MEASURED
#: 2026-09-07 over 203 files in ~/.claude/plans/: 11 exceed 65_536, and every one
#: of them was being judged on its first 64KB.
#:
#: The lesson worth keeping: reusing a bounded helper INHERITS ITS BOUND. Read the
#: constant you are importing, or your own is fiction.
_MAX_PLAN = 65_536

#: A confidence figure: "88%", "88 %", "70-90%". One to three digits so a stray
#: long number cannot masquerade as one.
_PERCENT_RE = re.compile(r"\b\d{1,3}[^\S\n]*%")

#: NOT a blocking criterion -- see the module docstring. Kept only to decide
#: whether the reminder should ALSO mention diligence, never whether to bounce.
#: Judging a plan by whether it contains the word "measured" is the validator
#: design this module deliberately is not.
_DILIGENCE_WORDS = (
    "measured",
    "verified",
    "disproven",
    "disprove",
    "falsifi",
    "enumerat",
    "due diligence",
    "unverified",
)

#: The opt-out, mirroring `E2E: none - <reason>`. Markdown-tolerant and
#: line-START anchored for the same reason the sibling is: prose that merely
#: mentions the marker must not satisfy it. Any separator an author might type is
#: accepted -- requiring one specific dash misclassified every natural phrasing in
#: the sibling module, which is a defect it had to find by mutation.
_CONF_NONE_RE = re.compile(
    r"^[^\S\n]*(?:[-*+>][^\S\n]*)*(?:\[[ xX]\][^\S\n]*)?"
    r"[*_`]{0,2}confidence[*_`]{0,2}[^\S\n]*:[^\S\n]*"
    r"none\b[^\S\n]*(?:(?:[—–\-:,;.]|\bbecause\b)[^\S\n]*(.*))?$",
    re.MULTILINE | re.IGNORECASE,
)

#: Same floor and refusal set as the sibling declaration parser, for the same
#: reason: `none` with no real reason is the decision left unmade wearing the
#: grammar of a decision.
_MIN_SUBSTANCE = 7
_REFUSAL_WORDS = frozenset({"todo", "tbd", "pending", "n/a", "na", "yes", "no", "?", "none"})


@functools.cache
def _load_readable_body():
    """`readable_body` from scripts/e2e_declaration.py, or None.

    Same lazy, failure-tolerant shape as `git_push_guard._load_e2e_declaration`,
    including registering the module in sys.modules BEFORE exec and popping it on
    failure -- a half-initialised entry poisons a later real import, and the two
    loaders disagreeing about that hygiene is how one of them ends up wrong.

    This matters more here than it looks: a PLAN is markdown full of fenced code
    blocks, and this guard's own examples live in fences. Without the strip, a
    fenced illustration of a confidence line would satisfy the gate.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    path = os.path.join(repo_root, "scripts", "e2e_declaration.py")
    name = "_e2e_declaration_for_plan_guard"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise
        fn = getattr(mod, "readable_body", None)
        return fn if callable(fn) else None
    except Exception:
        return None


def readable_plan(plan: str) -> str:
    """The part of a plan that is prose rather than illustration.

    Degrades to the raw text when the sibling cannot be loaded. That direction is
    the safe one for a fail-open guard: unstripped text can only ever satisfy MORE
    signals, so the failure mode is a plan that passes, never one wrongly refused.
    """
    fn = _load_readable_body()
    if fn is None:
        return plan
    try:
        return fn(plan)
    except Exception:
        return plan


def _reason_is_real(value: str) -> bool:
    cleaned = value.strip("*_`~ \t")
    normalised = cleaned.lower().strip(" .-—–")
    if not normalised or normalised in _REFUSAL_WORDS:
        return False
    if re.fullmatch(r"<[^<>]+>", normalised):
        return False
    return sum(ch.isalnum() for ch in cleaned) >= _MIN_SUBSTANCE


def confidence_gaps(plan: str | None) -> list[str]:
    """Which required signals a plan is missing. Empty list == the plan passes.

    A PURE function over a string, so a positive control can feed it synthetic
    plans -- otherwise "no gaps found" and "no detector" are the same result, which
    is the failure this repo has paid for more than once.

    Returns a list of short gap names: "confidence", "diligence".
    """
    if not plan or not plan.strip():
        return []  # nothing to judge; the caller fails open.
    if len(plan) > _MAX_PLAN:
        # Cannot read the whole thing, so cannot honestly say a signal is ABSENT.
        # "Not found in a prefix" is not absence -- the same rule CLAUDE.md states
        # for truncated listings. Fail open rather than refuse on a partial read.
        return []
    text = readable_plan(plan)

    gaps: list[str] = []

    has_percent = bool(_PERCENT_RE.search(text))
    has_optout = any(
        _reason_is_real(m.group(1) or "") for m in _CONF_NONE_RE.finditer(text)
    )
    if not (has_percent or has_optout):
        gaps.append("confidence")

    # Diligence is REPORTED, never a reason to bounce on its own. A plan carrying
    # "92% confident - I traced every caller and read chain.py end to end" states
    # real diligence and contains none of the words above; blocking it would be
    # lexical conformance wearing a gate's clothes, and MEASURED 2026-09-07 that
    # is 21/203 plans on this install.
    if "confidence" in gaps and not any(w in text.lower() for w in _DILIGENCE_WORDS):
        gaps.append("diligence")

    return gaps


#: What the guard prints when it refuses. Names BOTH valid forms and gives a
#: CONCRETE copyable line for each, because an author blocked by a gate needs a
#: line they can type -- and `test_every_example_in_the_guidance_passes_the_detector`
#: runs every concrete example here back through `confidence_gaps`, so this gate can
#: never prescribe a remedy it would itself refuse.
GUIDANCE = (
    "Before this plan goes to the user, state your confidence and your due "
    "diligence -- CLAUDE.md's Confidence Framework asks for both, and this is the "
    "standing reminder for it.\n"
    "  - confidence, per item, with rationale and what would change it, e.g.\n"
    "      Item A - route the hook: 85% confident; DISPROVEN if the probe shows "
    "the event does not fire.\n"
    "  - what you actually checked, and what you did NOT, e.g.\n"
    "      MEASURED: 7 callers across 5 modules (Serena find_referencing_symbols).\n"
    "      NOT verified: whether the deploy path has run on this install.\n"
    "If confidence genuinely does not apply, say so explicitly instead:\n"
    "      Confidence: none - pure documentation edit, no runtime surface\n"
    "A reasoned `none` is a legitimate answer; leaving the decision unmade is not."
)


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    try:
        import json

        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    # Scoping is intrinsic, not inherited from the settings matcher: a broadened
    # matcher (or a change in CC's matcher semantics) must not turn this into a
    # gate on any tool that happens to carry a `plan` key. None is allowed so a
    # hand-fed payload in a test is not silently a no-op.
    name = payload.get("tool_name")
    if name is not None and name != "ExitPlanMode":
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    plan = tool_input.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        # A guard that cannot read its subject must not block on a guess.
        return 0

    gaps = confidence_gaps(plan)
    if not gaps:
        return 0

    named = " and ".join(gaps)
    where = tool_input.get("planFilePath")
    location = f"\nPlan file: {where}" if isinstance(where, str) and where else ""
    print(
        f"PLAN REFUSED - missing: {named}.{location}\n\n{GUIDANCE}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail OPEN. See the module docstring: a crash here must never be able to
        # refuse every plan in every session.
        sys.exit(0)
