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
from typing import NamedTuple

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

#: `gh api`'s option grammar, MEASURED from `gh api --help` (gh 2.101.0,
#: consulted 2026-09-20) rather than recalled, per the house rule that a claim
#: about an external tool's flags is a lookup and not a memory.
#:
#: It is scoped to the `api` GROUP and deliberately NOT merged into
#: `shell_parse`'s shared `gh` spec. That is a different thing from the second
#: copy the doctrine forbids: gh's option grammar is PER-SUBCOMMAND, and the
#: same short flag means different things under different groups. MEASURED:
#: `gh api -f` is `--raw-field` and TAKES a value, while `gh pr create -f` is
#: `--fill` and takes none. Adding `-f` to the shared `value_flags` would
#: therefore mis-parse `gh pr create -f` for every consumer of that spec, the
#: fail-closed merge gate included. The shared spec models what is common to
#: all of gh (`-R/--repo`); this models one group that it does not.
_API_VALUE_FLAGS = frozenset(
    {
        "--cache",
        "-F",
        "--field",
        "-H",
        "--header",
        "--hostname",
        "--input",
        "-q",
        "--jq",
        "-X",
        "--method",
        "-p",
        "--preview",
        "-f",
        "--raw-field",
        "-t",
        "--template",
    }
)

#: The two spellings that carry a `key=value` request parameter.
_API_FIELD_FLAGS = frozenset({"-f", "--raw-field", "-F", "--field"})

#: Value-taking flags for the `pr close` group, MEASURED from
#: `gh pr close --help` (gh 2.101.0): `-c/--comment` takes a string and
#: `-R/--repo` a repo. The shared spec knows only `-R`, and without `-c`
#: here a closing COMMENT whose text is a help flag would read as a help
#: invocation and silence a real close -- a false NEGATIVE introduced by a
#: false-positive fix, which is the worse of the two directions.
_PR_CLOSE_VALUE_FLAGS = frozenset({"-c", "--comment", "-R", "--repo"})

#: MEASURED: `gh api repos/octocat/hello-world --help` prints help and issues no
#: request, so a terminal help flag ANYWHERE means the command performs nothing.
_HELP_FLAGS = frozenset({"--help", "-h"})

#: The mutation, tested against the VALUE of the `query` field rather than
#: against rejoined argv. The previous form searched the whole command and so
#: could begin inside one field and end inside another: MEASURED, a
#: `query=` holding `mutation { createIssue(...) }` beside a `body=` holding
#: the mutation NAME reported a close the query never performs. Requiring
#: `mutation` separates a close from an introspection query that merely names
#: the field.
_GRAPHQL_CLOSE = re.compile(r"\bmutation\b.*?closePullRequest", re.IGNORECASE | re.DOTALL)

#: The REST spelling, matched against the ENDPOINT ARGUMENT alone. It used to be
#: searched across rejoined argv, where any field value that looked like a path
#: matched: MEASURED, a `graphql` call carrying `-f body=repos/o/r/pulls/5`
#: beside `-f state=closed` fired on prose. `fullmatch` against the endpoint
#: makes a path that is merely mentioned unrepresentable rather than unlikely.
#:
#: Anchoring at the end also keeps the two MEASURED sub-resource negatives:
#: `…/pulls/5/reviews` and `…/pulls/5?state=closed` are a listing and a filtered
#: GET, neither of which closes anything.
_REST_PATH = re.compile(r"(?:https?://[^/]+/)?repos/[^/\s]+/[^/\s]+/(?P<kind>pulls|issues)/\d+/?")


def _split_api_option(tok: str) -> tuple[str, str | None]:
    """`-fstate=closed` -> ('-f', 'state=closed'); `--field=x=1` -> ('--field', 'x=1').

    Only a flag KNOWN to take a value absorbs a glued remainder. Without that
    check a boolean short and a value-bearing one are indistinguishable, and
    the attached forms are exactly where the old suffix test went wrong: it
    accepted ANY dashed token ending in `state=closed`, so an output template
    spelled `--template=state=closed` read as a close.
    """
    if tok.startswith("--"):
        name, sep, val = tok.partition("=")
        return name, (val if sep else None)
    if len(tok) > 2 and tok[:2] in _API_VALUE_FLAGS:
        return tok[:2], tok[2:]
    return tok, None


def _gh_group_at(argv: list[str]) -> tuple[int, str] | None:
    """(index, GROUP word) for a gh argv, or None.

    The option table is read off `shell_parse`'s own dispatcher spec rather than
    restated, which is the instruction `gh_pr_subcommand` leaves in its body:
    *"a second copy of one CLI option grammar is the shape that produced the
    defect this change is about."* That defect was a separated `--repo o/r`
    whose VALUE got read as the subcommand.

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
    for i, tok in enumerate(argv[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        name, attached = _option_name(tok, spec)
        if not attached and name in spec.value_flags:
            skip_next = True
            continue
        if not tok.startswith("-"):
            return i, tok
    return None


def _gh_group(argv: list[str]) -> str | None:
    """The gh GROUP word (`pr`, `run`, `label`, …) for a gh argv, or None."""
    hit = _gh_group_at(argv)
    return hit[1] if hit else None


def _has_terminal_help(argv: list[str], value_flags: frozenset[str]) -> bool:
    """Is a help flag present as a FLAG rather than as some option's value?

    A plain `tok in argv` scan would read a field whose CONTENT is `--help` as
    help and go silent on a real close. Skipping each value-flag's argument is
    what keeps a field's content from deciding whether the command runs.
    """
    skip_next = False
    for tok in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        name, value = _split_api_option(tok)
        if name in _HELP_FLAGS:
            return True
        if value is None and name in value_flags:
            skip_next = True
    return False


class _ApiCall(NamedTuple):
    endpoint: str | None
    fields: tuple[tuple[str, str], ...]
    method: str | None


def _parse_api(argv: list[str]) -> _ApiCall | None:
    """Structured read of a `gh api` argv, or None if this is not one.

    `api` must be the GROUP, not merely a token present somewhere: MEASURED, a
    `gh workflow run api …` invocation runs a WORKFLOW named `api`, and the old
    membership test (`"api" in argv[1:]`) claimed it closed a PR.
    """
    hit = _gh_group_at(argv)
    if hit is None or hit[1] != "api":
        return None
    endpoint: str | None = None
    fields: list[tuple[str, str]] = []
    method: str | None = None
    positional_only = False
    i = hit[0] + 1
    while i < len(argv):
        tok = argv[i]
        if positional_only or not tok.startswith("-") or tok == "-":
            if endpoint is None:
                endpoint = tok
            i += 1
            continue
        if tok == "--":
            positional_only = True
            i += 1
            continue
        name, value = _split_api_option(tok)
        if value is None and name in _API_VALUE_FLAGS:
            i += 1
            value = argv[i] if i < len(argv) else None
        if value is not None:
            if name in _API_FIELD_FLAGS:
                key, sep, val = value.partition("=")
                if sep:
                    fields.append((key, val))
            elif name in ("-X", "--method"):
                method = value
        i += 1
    return _ApiCall(endpoint, tuple(fields), method)


#: Printed INSIDE the advisory, never only here. A reader who learns the
#: boundary in a docstring learns it somewhere they are not standing when they
#: rely on the check.
_LIMIT = (
    "This note reads the command text only. A mutation supplied on stdin "
    "(`--input -`) or from a file (`query=@file`) is not visible here and "
    "produces no note — so silence is not evidence that a command leaves the "
    "PR open."
)


def _closes_a_pr(argv: list[str]) -> str | None:
    """Why this argv appears to close a PR, or None. Text-visible forms only."""
    if not argv or os.path.basename(argv[0]) != "gh":
        return None
    spec = _VERB_DISPATCHERS.get("gh")
    shared_flags = spec.value_flags if spec is not None else frozenset()
    known_values = frozenset(_API_VALUE_FLAGS | _PR_CLOSE_VALUE_FLAGS | set(shared_flags))
    if _has_terminal_help(argv, known_values):
        return None
    # `gh_pr_subcommand` is REUSED rather than re-derived: it already survived a
    # real bypass (a separated repo flag whose VALUE was read as the subcommand,
    # skipping every downstream gate), and a second copy of that grammar is how
    # the two drift apart.
    #
    # But it is reused with an ADDED narrowing, because the cost model inverts
    # here. It scans for a `pr` token ANYWHERE in argv, which is the safe
    # direction for the fail-closed merge gate it was written for -- over-
    # matching there costs a prompt. Over-matching HERE is the failure this hook
    # names as fatal to itself. MEASURED false positives: a `gh run list`
    # naming a workflow, a `gh label create`, and a `gh alias set` whose
    # operands happened to contain those words. An advisory additionally
    # requires the group to be the first positional, which is the only shape
    # that is really a `pr` subcommand.
    if gh_pr_subcommand(argv) == "close" and _gh_group(argv) == "pr":
        return "`gh pr close`"
    call = _parse_api(argv)
    if call is None or call.endpoint is None:
        return None
    if call.endpoint == "graphql":
        if any(k == "query" and _GRAPHQL_CLOSE.search(v) for k, v in call.fields):
            return "a `closePullRequest` GraphQL mutation"
        return None
    rest = _REST_PATH.fullmatch(call.endpoint)
    if rest is None:
        return None
    # MEASURED from `gh api --help`: the method defaults to GET, and to POST
    # when any parameter is added -- never to PATCH. So a close REQUIRES the
    # method to be stated, and an endpoint-plus-field command with no `-X` is a
    # POST that closes nothing. Reading the verdict off the path and the field
    # alone reported exactly that as a close.
    if (call.method or "").upper() != "PATCH":
        return None
    if not any(k == "state" and v == "closed" for k, v in call.fields):
        return None
    if rest.group("kind") == "issues":
        # The issues endpoint addresses BOTH issues and pull requests, and the
        # number alone does not say which. Naming both is the honest form; the
        # old text asserted "pull request" for what is usually an issue.
        return "a REST `state=closed` PATCH to an issue-or-pull-request endpoint"
    return "a REST `state=closed` PATCH to a pull-request endpoint"


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
    plural = "pull requests or issues" if ambiguous else "pull requests"
    via = " and ".join(reasons)
    lead = (
        f"This command closes a {singular}, via {via}."
        if closes == 1
        else f"This command closes {closes} {plural}, via {via}."
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
    closes = 0
    for seg in segments:
        why = _closes_a_pr(list(seg.argv or []))
        if not why:
            continue
        closes += 1
        if why not in reasons:
            reasons.append(why)
    if reasons:
        print_json_bounded(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _advisory(reasons, closes),
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
