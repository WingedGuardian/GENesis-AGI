#!/usr/bin/env python3
"""Text-visible pull-request close reasons for approval-gate messages.

The parser can miss a close supplied through input the command text cannot see.
A false negative omits close context; a false positive can only name a possible
close in the message. Over-match is the safe side for gate callers, which must
not use this result to decide whether a command is allowed or refused.
"""

from __future__ import annotations

import os
import re
import sys
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shell_parse import (  # noqa: E402
    _GH_ALL_VALUE_FLAGS,
    _GH_FLAG_TABLE,
    gh_command,
)

_API_FIELD_FLAGS = frozenset({"-f", "--raw-field", "-F", "--field"})

#: MEASURED: `gh api repos/octocat/hello-world --help` prints help and issues no
#: request, so a terminal help flag ANYWHERE means the command performs nothing.
_HELP_FLAGS = frozenset({"--help", "-h"})

#: Every value-taking flag this predicate must step over, from ANY of the groups
#: it reads — the union across all modeled rows, because MEASURED against the
#: real CLI that is what gh itself applies while the command path is still
#: unresolved: `gh -X PATCH api --help` and `gh -c note pr close --help` both
#: resolve, and `gh -f pr create --help` FAILS with `unknown command
#: "create"` -- gh having eaten `pr` as the value of `-f`. So a parser that
#: does not step over these mislocates the group exactly where gh does not.
_VALUE_FLAGS = _GH_ALL_VALUE_FLAGS

#: `gh api`'s OWN row of the shared table. The union above is right while the
#: command path is unresolved — that is gh's own behaviour — but it is WRONG
#: after `api` resolves: `-i` carries a value under `pr checks` yet is the
#: valueless `--include` under `api`, so a union-only scan reads
#: `gh api … -i -X PATCH` as `-i` consuming `-X` and hides the PATCH (Devin
#: Review on #2256). `GhInvocation.path_end` marks the switch point.
_API_VALUE_FLAGS = _GH_FLAG_TABLE[("api", "")][0]

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
#: The optional leading slash is MEASURED, not defensive: `gh api --help`
#: (gh 2.101.0) demonstrates endpoints in exactly that spelling, so
#: `gh api /repos/o/r/pulls/5 -X PATCH -f state=closed` is a documented close
#: that the anchored pattern silently missed.
_REST_PATH = re.compile(r"(?:https?://[^/]+/)?/?repos/[^/\s]+/[^/\s]+/(?P<kind>pulls|issues)/\d+/?")


def _split_api_option(tok: str, value_flags: frozenset[str]) -> tuple[str, str | None]:
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
    if len(tok) > 2 and tok[:2] in value_flags:
        # `-X=PATCH` as well as `-XPATCH`: the separator is optional in the
        # shorthand form, and keeping the `=` in the value made the method
        # compare as "=PATCH" and never match.
        return tok[:2], tok[2:].lstrip("=") or None
    return tok, None


def _gh_group(argv: list[str]) -> str | None:
    """The gh GROUP word (`pr`, `run`, `label`, …) for a gh argv, or None."""
    inv = gh_command(argv)
    return inv.group if inv else None


def _has_terminal_help(argv: list[str], inv) -> bool:
    """Is a help flag present as a FLAG rather than as some option's value?

    A plain `tok in argv` scan would read a field whose CONTENT is `--help` as
    help and go silent on a real close. Skipping each value-flag's argument is
    what keeps a field's content from deciding whether the command runs — and
    the flag set doing that skipping switches at `inv.path_end` (union before
    the path resolves, the resolved row after), or `api -i --help` reads
    `--help` as `-i`'s value and a help-only command looks like a close.
    """
    row = _GH_FLAG_TABLE.get((inv.group, inv.subcommand or ""), (frozenset(), frozenset()))
    post_path_flags = row[0] if inv.path_end else _VALUE_FLAGS
    skip_next = False
    for i, tok in enumerate(argv[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        vf = _VALUE_FLAGS if i < inv.path_end else post_path_flags
        name, value = _split_api_option(tok, vf)
        if name in _HELP_FLAGS:
            return True
        if value is None and name in vf:
            skip_next = True
    return False


class _ApiCall(NamedTuple):
    endpoint: str | None
    fields: tuple[tuple[str, str], ...]
    method: str | None
    #: The `--input` value (a path, or `-` for stdin). It makes the request a
    #: POST when no method is given, and it carries the whole body.
    input: str | None = None


def _parse_api(argv: list[str]) -> _ApiCall | None:
    """Structured read of a `gh api` argv, or None if this is not one.

    `api` must be the GROUP, not merely a token present somewhere: MEASURED, a
    `gh workflow run api …` invocation runs a WORKFLOW named `api`, and the old
    membership test (`"api" in argv[1:]`) claimed it closed a PR.
    """
    inv = gh_command(argv)
    if inv is None or inv.group != "api":
        return None
    # The endpoint is the first POSITIONAL after the command path, resolved by
    # the shared walk rather than a local one. The OPTION scan below still
    # covers all of argv, because gh accepts api-local flags on BOTH sides of
    # the group -- MEASURED: `gh -X PATCH api ...` resolves. Only the ENDPOINT
    # is positional; the options belong to the invocation wherever they sit.
    endpoint = inv.positionals[0] if inv.positionals else None
    fields: list[tuple[str, str]] = []
    method: str | None = None
    body_input: str | None = None
    positional_only = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        if positional_only or not tok.startswith("-") or tok == "-":
            i += 1
            continue
        if tok == "--":
            positional_only = True
            i += 1
            continue
        vf = _VALUE_FLAGS if i < inv.path_end else _API_VALUE_FLAGS
        name, value = _split_api_option(tok, vf)
        if value is None and name in vf:
            i += 1
            value = argv[i] if i < len(argv) else None
        if value is not None:
            if name in _API_FIELD_FLAGS:
                key, sep, val = value.partition("=")
                if sep:
                    fields.append((key, val))
            elif name in ("-X", "--method"):
                method = value
            elif name == "--input":
                body_input = value
        i += 1
    return _ApiCall(endpoint, tuple(fields), method, body_input)


def pr_close_reason(argv: list[str]) -> str | None:
    """Why this argv appears to close a PR, or None. Text-visible forms only."""
    if not argv or os.path.basename(argv[0]) != "gh":
        return None
    inv = gh_command(argv)
    if inv is not None and _has_terminal_help(argv, inv):
        return None
    # The group/subcommand comes from `shell_parse.gh_command`, the shared
    # resolver every consumer now uses (#2209) — a hand-rolled walk here was
    # the second recurrence of the same defect: a `first token not starting
    # with -` locator returned `owner/repo` for `gh --repo owner/repo pr
    # close 1` and silenced a real close. Requiring the group position keeps
    # the measured negatives (`gh run list` naming a workflow, `gh label
    # create`, `gh alias set` carrying the words as operands) unreachable.
    if inv is not None and inv.group == "pr" and inv.subcommand == "close":
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
