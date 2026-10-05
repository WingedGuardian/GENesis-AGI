#!/usr/bin/env python3
"""Closing a pull request is the user's decision. Say so; never block it.

WHAT GAP THIS FILLS
===================
There IS a rule. `genesis-development/SKILL.md` carries a 30-line standing user
rule, "Never RETIRE a PR you are not the one reviving": a reviewer session
retires nothing, a premise-wrong PR gets `needs-architecture-session` and stays
OPEN, a superseded one gets a comment naming its successor and also stays open,
and retiring belongs to the session taking up the revival.

**Nothing enforces or surfaces it.** MEASURED against `origin/main`: no hook
mentions PR closure at all except one line in the push guard, and that one is a
push gate defending itself — `gh pr close <n> && git push` invalidates a
push-allow, because the hook runs before any of the command does and would
otherwise publish into the PR-less state the ask exists to report.

So the rule is reachable only by a session that happened to load the skill. That
is the gap: not an absent policy, an unreachable one. This hook is the
chokepoint — the note fires at the moment the command is typed, which is the one
moment the rule binds. It is the repo's own convention-to-chokepoint pattern:
an obligation every call site must REMEMBER is an obligation that will be missed.

(The first version of this docstring claimed nothing anywhere mentioned closing
a PR. That was false, and it was false in the direction that flatters the
change — an unmeasured claim of novelty. The true justification is stronger, so
the correction cost nothing but the sentence.)

WHY THIS IS ADVISORY, AND WHY THAT IS NOT A WEAKER VERSION
==========================================================
Owner decision, 2026-09-20, and it is the decision that shapes everything here.

An earlier and much larger attempt at this enforced: ask in a foreground
session, deny in a dispatched one, plus a written rebuild-commitment required
before closing a PR that had exhausted its review rounds. It drew eleven review
findings, and eight of them were in two classes this repo has already paid for.

FIVE were one question — *can the gate tell, from the command text, whether this
command closes a PR?* It cannot, and not for want of trying: `gh api graphql`
takes its mutation from `--input -` (stdin) or `query=@file`, and neither is in
argv at all. The finding was fixed once and came straight back. Enforcement
makes that gap a BYPASS, so it has to be closed, and it cannot be.

Advisory dissolves it. A note that misses a form is a note that did not fire.
Nothing is bypassed, because nothing was blocked. The undecidable case stops
being a correctness problem and becomes a coverage one, which a sentence can
honestly describe — see `_LIMIT` below, which is printed IN the advisory rather
than buried here, so the reader learns the boundary at the moment they rely on
it.

THREE more were the lifecycle of the commitment store: its filename, its
syntax, its backup. New persisted state around a gate is this repo's most
expensive documented shape. Advisory has no dialog to gate, so there is nothing
to persist and the class does not arise.

The standing axioms land the same way independently: advisory is the default;
escalating to a block needs a specific, MEASURED reason, and there is no
measurement here — zero incidents of an unwanted close are known. A background
session must stay as capable as a foreground one, and "ask" in a session with
no human present is a block nobody intended.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_input import read_payload, tool_input  # noqa: E402
from hook_output import print_json_bounded  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze_checked,
    gh_command,
    mentions,
    pr_close_reason,
)

_closes_a_pr = pr_close_reason

#: Cheap prefilter, same reasoning as `capped_read_advisory._GH_WORD`: this runs
#: on EVERY Bash call and a bare "gh" substring matches "through" and "high", so
#: an untightened check would send nearly every command through the parser. The
#: lookarounds exclude word characters and "-" but NOT "/", so an absolute
#: invocation (`/usr/bin/gh pr close 1`) still reaches the parse — `seg.exe`
#: resolves on the basename and would match it.
_GH_WORD = re.compile(r"(?<![\w-])gh(?![\w-])")

def _gh_group(argv: list[str]) -> str | None:
    """The gh GROUP word (`pr`, `run`, `label`, …) for a gh argv, or None."""
    inv = gh_command(argv)
    return inv.group if inv else None


#: Printed INSIDE the advisory, never only here. A reader who learns the
#: boundary in a docstring learns it somewhere they are not standing when they
#: rely on the check.
_LIMIT = (
    "This note reads the command text only. A mutation supplied on stdin "
    "(`--input -`) or from a file (`query=@file`) is not visible here, nor is "
    "a close reached through a shell alias, an expanded executable path, or "
    "the body of a quoted heredoc — none of those produce a note, so silence "
    "is not evidence that a command leaves the PR open."
)


def _advisory(reasons: list[str], closes: int) -> str:
    """`reasons` are the DISTINCT mechanisms; `closes` is how many were seen.

    The two are counted separately because deduplicating by mechanism erases
    repetition: two closes spelled the same way collapse to one reason, and a
    lead that took its number from the reason LIST then reported a compound
    retirement of several PRs as though it touched one.

    The subject stays "a pull request or issue" for the issues endpoint,
    which addresses both and says which nowhere in the command text. The
    standing rule is about PRs, so asserting one here would be the advisory
    inventing the fact that makes it relevant.
    """
    ambiguous = any("issue-or-pull-request" in r for r in reasons)
    singular = "pull request or issue" if ambiguous else "pull request"
    via = " and ".join(reasons)
    # Counted in STEPS, and said that way. The number is how many parsed
    # steps close something, which is not the number of PRs: a `for` loop is
    # ONE step that may close many, and `a || b` is two steps of which at most
    # one runs. Claiming a PR count from a step count was wrong in both
    # directions at once.
    lead = (
        f"This command closes a {singular}, via {via}."
        if closes == 1
        else f"This command has {closes} steps that close a {singular}, via {via}."
    )
    return (
        f"NOTE: {lead}\n"
        "STANDING RULE (genesis-development, 'Never RETIRE a PR you are not the "
        "one reviving'): a reviewer session retires nothing. A PR that is wrong "
        "at the premise gets the `needs-architecture-session` label carrying the "
        "evidence, and STAYS OPEN — retiring belongs to the session that takes "
        "up its revival. A superseded PR gets a comment naming its successor and "
        "also stays open. If you believe one should be retired and nobody is "
        "picking it up, that is a question for the user.\n"
        "So: if you are the reviving session, or the user asked for this close "
        "by name, proceed. Otherwise say what you are about to close and why, "
        "and let them answer.\n"
        f"{_LIMIT}"
    )


#: The three close spellings this hook covers, named in the text of a command it could
#: not parse: `pr close`, a `closePullRequest` mutation, a `state=closed` field. Only
#: decides whether that command gets a one-line note. MEASURED over 86,684 recorded
#: commands: a bare `close` substring put the note on 30 continued commands, 29 of them
#: PR bodies and review replies whose prose says "close"; these spellings leave 1.
_CLOSE_WORD = re.compile(r"\bpr\s+close\b|closePullRequest|state=closed")


def _unreadable_note(blind) -> str:
    """The short note for a command the parse could not read (built from the blind
    spot's own cause and remedy, so it is true for every bounds-type cause)."""
    return (
        f"NOTE: this command {blind.cause}, so I could not check whether it closes a "
        "pull request; it mentions a close. If it does: closing a PR is the user's "
        "decision unless you are the session reviving it (genesis-development, "
        "'Never RETIRE a PR you are not the one reviving'). For the specific check: "
        f"{blind.hint}."
    )


def _scan(segments: list) -> tuple[list[str], int]:
    """The close reasons and the number of closing steps in ONE reading."""
    reasons: list[str] = []
    closes = 0
    for seg in segments:
        # depth>0 is substitution or a QUOTED-HEREDOC BODY. A quoted delimiter
        # suppresses expansion in bash, so that text is prose the shell never
        # runs -- `shell_parse` parses it anyway, which is the right
        # fail-closed posture for a destructive guard and the wrong one for an
        # advisory whose stated fatal failure is noise.
        #
        # MEASURED over 57,445 unique real Bash commands: 36 fire, and 4 of
        # those are false positives, every one of them a heredoc or `$(...)`
        # containing prose ABOUT closing a PR -- three of them written while
        # developing this very hook. Skipping depth>0 removes 4 of 4 and loses
        # 0 of 32 true positives on the same corpus.
        #
        # THE COST, stated rather than discovered later: a genuine
        # `bash -c 'gh pr close 1'` or `X=$(gh pr close 1)` is also depth 1 and
        # is now missed. Neither occurs in those 57,445 commands, but both are
        # real shapes. Separating "nested but executed" from "nested inside a
        # quoted heredoc" needs a distinction `shell_parse` does not model
        # today, so this trades a measured noise class for an unmeasured
        # coverage one -- the right direction for an advisory, and `_LIMIT`
        # already tells the reader silence is not evidence.
        if seg.depth:
            continue
        why = pr_close_reason(list(seg.argv or []))
        if not why:
            continue
        closes += 1
        if why not in reasons:
            reasons.append(why)
    return reasons, closes


def _process(payload: dict) -> None:
    cmd = (tool_input(payload) or {}).get("command") or ""
    if not cmd or not mentions(cmd, _GH_WORD):
        return
    segments, blind = analyze_checked(cmd)
    # THE BLIND FLAG IS NOT A REASON FOR SILENCE. The UNTOKENIZABLE blind spot
    # still returns segments, and `gh pr close 'unterminated` is among them -- a
    # genuine close attempt a flag check would silence. A BOUNDS-TYPE one returns
    # none, which used to be harmless because the bounds are measured at 0 of
    # 45,956 real commands; a line continuation is also bounds-type and is
    # ordinary input, and its segments are not what the shell runs. Re-parsing the
    # join to find the close was tried, and review found a new defect in that
    # modelling each round (a close after a comment, a close counted once per
    # reading). So a continued command that names a close gets a SHORT note from
    # the text alone, and no count. Over-long or over-nested commands take this
    # branch too; the note names each one's own cause.
    if blind is not None and blind.bounds_induced:
        if mentions(cmd, _CLOSE_WORD):
            _emit(_unreadable_note(blind))
        return
    reasons, closes = _scan(segments)
    # Advising on what DID parse is right for an advisory: a spurious note costs a
    # sentence, where a fail-closed guard in the same position must refuse,
    # because for it a spurious ALLOW costs a bypass. `_LIMIT` already tells the
    # reader that silence is not evidence, which covers whatever the parser could
    # not reach.
    if reasons:
        _emit(_advisory(reasons, closes))


def _emit(context: str) -> None:
    print_json_bounded(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": context,
            }
        },
        text_keys=("hookSpecificOutput.additionalContext",),
    )


def main() -> int:
    # Advisory: fail OPEN, always exit 0. Never `run_guard` — that is
    # fail-CLOSED and its own docstring reserves it for irreversible guards.
    #
    # The exception goes to stderr, which a PreToolUse hook exiting 0 does NOT
    # show the model, so a bug here costs the session nothing and still lands in
    # the harness log for whoever is debugging a quiet hook.
    try:
        _process(read_payload())
    except Exception as exc:  # noqa: BLE001 - advisory: never block on our own bug
        print(f"pr_close_advisory: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
