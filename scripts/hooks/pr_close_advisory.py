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
    _VERB_DISPATCHERS,
    _option_name,
    analyze_checked,
    gh_pr_subcommand,
)

#: Cheap prefilter, same reasoning as `capped_read_advisory._GH_WORD`: this runs
#: on EVERY Bash call and a bare "gh" substring matches "through" and "high", so
#: an untightened check would send nearly every command through the parser. The
#: lookarounds exclude word characters and "-" but NOT "/", so an absolute
#: invocation (`/usr/bin/gh pr close 1`) still reaches the parse — `seg.exe`
#: resolves on the basename and would match it.
_GH_WORD = re.compile(r"(?<![\w-])gh(?![\w-])")

#: A GraphQL mutation that closes a pull request, as it appears in argv.
#:
#: Scoped to a `query=` FIELD rather than searched across the whole argv, and
#: that scoping is not fussiness: MEASURED false positives included
#: `gh api ... -f body='we should use closePullRequest here'` and
#: `-f title='fix: state=closed parsing'`. A session commenting on this very
#: feature through `gh api` tripped its own advisory. Requiring `mutation`
#: inside the same token additionally separates a close from an introspection
#: query that merely names the field.
#:
#: Case-insensitive on the field name only: the surrounding query may be
#: whitespaced any number of ways, and anchoring on more of it would fail on
#: formatting rather than on meaning.
_CLOSE_MUTATION = re.compile(
    r"(?:^|[\s'\"])(?:-f|-F|--field|--raw-field)?=?\s*query=.*?\bmutation\b.*?closePullRequest",
    re.IGNORECASE | re.DOTALL,
)

#: The REST spelling: a field `state=closed` on a pull/issue endpoint. `gh`
#: routes `pulls/N` and `issues/N` to the same object and closing through the
#: issues path is documented, so both count.
#:
#: `\b` and not `(?:^|/)`: the path is preceded by a SPACE in every real
#: invocation (`gh api repos/o/r/pulls/5 ...`), so the anchored form matched
#: nothing at all — caught by the smoke matrix, which is why the negatives and
#: positives are both in it. `\b` still refuses `myrepos/...`, since `r` there
#: follows a word character.
#: The trailing `(?![/\w])` is load-bearing and was added after a MEASURED
#: false positive: `gh api repos/o/r/pulls/5/reviews?state=closed` READS a
#: PR's reviews with a state filter and fired the note. Closing targets the
#: BARE resource, so a path continuing into a sub-resource is not a close.
#: Noise is this hook's real failure mode — an advisory that cries wolf is
#: one nobody reads, which is indistinguishable from one that never fired.
_REST_PATH = re.compile(r"\brepos/[^/\s]+/[^/\s]+/(?:pulls|issues)/\d+(?![/\w])")


#: A `state=closed` FIELD, tested per token rather than against the joined
#: argv, because only the token knows whether the characters before `state`
#: are a flag or a word.
#:
#: Two MEASURED failures of the joined-regex version, in opposite directions.
#: `\b` missed `-fstate=closed` — the glued spelling the comment claimed to
#: cover — because the preceding character is the `f` of the flag. Widening the
#: lookbehind to admit it then matched
#: `-f title='fix: state=closed parsing'`, a PR retitle that mentions the
#: phrase. A token either IS the field or is a flag carrying it; nothing else
#: counts, and that distinction does not survive being joined with spaces.
#:
#: A query-string form (`…/pulls/5?state=closed`) is deliberately NOT a match:
#: that is a GET with a filter, which reads rather than closes.
def _is_state_closed_field(tok: str) -> bool:
    if tok == "state=closed":  # the separated form: `-f state=closed`
        return True
    # The glued and attached forms: `-fstate=closed`, `--field=state=closed`.
    return tok.startswith("-") and tok.lstrip("-").endswith("state=closed")


#: Printed INSIDE the advisory, never only here. A reader who learns the
#: boundary in a docstring learns it somewhere they are not standing when they
#: rely on the check.
_LIMIT = (
    "This note reads the command text only. A mutation supplied on stdin "
    "(`--input -`) or from a file (`query=@file`) is not visible here and "
    "produces no note — so silence is not evidence that a command leaves the "
    "PR open."
)


def _gh_group(argv: list[str]) -> str | None:
    """The gh GROUP word (`pr`, `run`, `label`, …) for a gh argv, or None.

    The option table is read off `shell_parse`'s own dispatcher spec rather than
    restated here, and that is not fastidiousness — it is the instruction
    `gh_pr_subcommand` leaves in its body for exactly this situation: *"a second
    copy of one CLI option grammar is the shape that produced the defect this
    change is about."* That defect was a separated `--repo o/r` whose VALUE got
    read as the subcommand.

    I reproduced it here, one level up, on the first attempt: a hand-rolled
    "first token not starting with `-`" returned `owner/repo` for
    `gh --repo owner/repo pr close 1` and silenced a real close. The test caught
    it. Reading the spec is what makes that unrepresentable rather than
    remembered.
    """
    spec = _VERB_DISPATCHERS.get("gh")
    if spec is None:  # pragma: no cover - the spec is module-level and static
        return None
    skip_next = False
    for tok in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        name, attached = _option_name(tok, spec)
        if not attached and name in spec.value_flags:
            skip_next = True
            continue
        if not tok.startswith("-"):
            return tok
    return None


def _closes_a_pr(argv: list[str]) -> str | None:
    """Why this argv appears to close a PR, or None. Text-visible forms only."""
    if not argv or os.path.basename(argv[0]) != "gh":
        return None
    # `gh_pr_subcommand` is REUSED rather than re-derived: it already survived a
    # real bypass (`gh pr -R o/r merge` let the separated repo flag's VALUE be read as
    # the subcommand, skipping every downstream gate), and a second copy of that
    # grammar is how the two drift apart.
    #
    # But it is reused with an ADDED narrowing, because the cost model inverts
    # here. It scans for a `pr` token ANYWHERE in argv, which is the safe
    # direction for the fail-closed merge gate it was written for -- over-
    # matching there costs a prompt. Over-matching HERE is the failure this hook
    # names as fatal to itself. MEASURED false positives: `gh run list
    # --workflow pr close`, `gh label create pr close`, `gh alias set prc --
    # pr close`. An advisory additionally requires `pr` to be the first
    # positional, which is the only shape that is really `gh pr <verb>`.
    if gh_pr_subcommand(argv) == "close" and _gh_group(argv) == "pr":
        return "`gh pr close`"
    if "api" not in argv[1:]:
        return None
    joined = " ".join(argv)
    if _CLOSE_MUTATION.search(joined):
        return "a `closePullRequest` GraphQL mutation"
    if _REST_PATH.search(joined) and any(_is_state_closed_field(t) for t in argv):
        return "a REST `state=closed` PATCH to a pull-request endpoint"
    return None


def _advisory(reasons: list[str]) -> str:
    lead = (
        f"This command closes a pull request, via {reasons[0]}."
        if len(reasons) == 1
        else f"This command closes pull requests, via {' and '.join(reasons)}."
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


def _process(payload: dict) -> None:
    cmd = (tool_input(payload) or {}).get("command") or ""
    if not cmd or not _GH_WORD.search(cmd):
        return
    segments, _blind = analyze_checked(cmd)
    # THE BLIND FLAG IS DELIBERATELY NOT CONSULTED, and an earlier comment here
    # claimed the opposite -- that a blind parse means silence. It did not, and
    # the two blind spots are why. The BOUNDS one returns no segments, so
    # silence is automatic and needs no flag. The UNTOKENIZABLE one still
    # returns segments, and `gh pr close 'unterminated` is among them -- a
    # genuine close attempt that a flag check would silence.
    #
    # So reading `_blind` would LOSE real closes to buy nothing, since the case
    # it would catch is already silent. Advising on what DID parse is right for
    # an advisory: a spurious note costs a sentence, where a fail-closed guard
    # in the same position must refuse, because for it a spurious ALLOW costs a
    # bypass. `_LIMIT` already tells the reader that silence is not evidence,
    # which covers whatever the parser could not reach.
    reasons: list[str] = []
    for seg in segments:
        why = _closes_a_pr(list(seg.argv or []))
        if why and why not in reasons:
            reasons.append(why)
    if reasons:
        print_json_bounded(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _advisory(reasons),
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
