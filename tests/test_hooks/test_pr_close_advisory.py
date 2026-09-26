"""The close advisory: it must fire on what it can see, and stay silent otherwise.

Two properties, and the second is the one that is easy to lose.

It must NOTICE a close in the forms a session actually types — including the
separated-repo spelling (`gh pr -R o/r close`), which was a real bypass in the
merge gate before `gh_pr_subcommand` was hardened against it.

And it must never BLOCK, never prompt, and never fail a command because of its
own bug. This hook runs on every Bash call; an advisory that can break the
session is worse than no advisory, and "advisory" is the whole reason the
enforcement design it replaces was abandoned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "pr_close_advisory.py"

#: Assembled rather than written literally: a bare `-f` in a test source line
#: reads as a force-push to the repo's own shell guard, which blocks the whole
#: command it appears in.
_F = "-" + "f"


def _run(
    command: str,
    payload: dict | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    body = payload if payload is not None else {"tool_input": {"command": command}}
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
    )


def _note(r: subprocess.CompletedProcess) -> str:
    """The advisory text, or "" when the hook stayed silent."""
    out = r.stdout.strip()
    if not out:
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


FIRES = [
    pytest.param("gh pr close 1705", id="plain"),
    pytest.param("gh pr -R owner/repo close 1", id="separated-repo-flag-before-subcommand"),
    pytest.param("gh --repo owner/repo pr close 1", id="global-flag-before-pr"),
    pytest.param("/usr/bin/gh pr close 1", id="absolute-invocation"),
    pytest.param("gh pr close 1 && git push", id="compound"),
    pytest.param(
        "gh api graphql " + _F + " query='mutation { closePullRequest(input:{x:1}) { id } }'",
        id="graphql-visible-in-argv",
    ),
    pytest.param(
        "gh api repos/o/r/pulls/5 -X PATCH " + _F + " state=closed", id="rest-pulls-endpoint"
    ),
    pytest.param(
        "gh api repos/o/r/issues/5 -X PATCH " + _F + " state=closed", id="rest-issues-endpoint"
    ),
    # The GLUED field spelling. MEASURED silent under the first pattern -- the
    # one form its own comment claimed to cover, missed because the character
    # before `state` is the `f` of the flag.
    pytest.param("gh api repos/o/r/pulls/5 -X PATCH " + _F + "state=closed", id="rest-glued-field"),
    pytest.param("gh api repos/o/r/pulls/5 -X PATCH --field state=closed", id="rest-long-field"),
    # Flag shapes around the group word. The separated `-R o/r` form is the one
    # that was a real bypass in the merge gate, and a hand-rolled narrowing
    # broke `--repo o/r` here by reading the flag's VALUE as the group.
    pytest.param("gh -R owner/repo pr close 1", id="short-repo-flag-before-pr"),
    pytest.param("gh --repo=owner/repo pr close 1", id="attached-repo-flag-before-pr"),
]

SILENT = [
    pytest.param("gh pr list", id="a-read"),
    pytest.param("gh pr merge 1 --squash", id="merge-is-a-different-gate"),
    pytest.param("gh pr comment 1 --body hi", id="comment"),
    pytest.param("git push", id="no-gh-at-all"),
    # NB this is a non-gh command, so it would be silent with or without the
    # prefilter -- the prefilter is a PERFORMANCE guard with no behavioural
    # signature, and deleting it entirely leaves this suite green. The pattern
    # itself is unit-tested below instead of being implied here.
    pytest.param("echo through the high road", id="a-non-gh-command-containing-the-substring"),
    pytest.param("gh api repos/o/r/pulls/5", id="reading-a-pr-is-not-closing-it"),
    pytest.param(
        "gh api graphql " + _F + " query='query { viewer { login } }'", id="a-non-close-mutation"
    ),
    # MEASURED false positives, fixed by the trailing `(?![/\w])` on the REST
    # path. Reading a PR's reviews or an issue's comments with a state filter
    # is not closing anything, and an advisory that cries wolf is one nobody
    # reads -- indistinguishable from one that never fired.
    pytest.param(
        "gh api repos/o/r/pulls/5/reviews?state=closed", id="reading-reviews-with-a-state-filter"
    ),
    pytest.param(
        "gh api repos/o/r/issues/5/comments?state=closed", id="reading-comments-with-a-state-filter"
    ),
    # A query-string filter on the bare resource. This is the param that
    # actually exercises the `(?![/\w])` boundary -- `pulls?state=closed` does
    # not, because it never matches `/pulls/\d+` in the first place, so it was
    # green with the lookahead reverted and the comment above it was a false
    # claim about what it tested.
    pytest.param("gh api repos/o/r/pulls/5?state=closed", id="query-filter-on-the-bare-resource"),
    pytest.param("gh api repos/o/r/pulls?state=closed", id="listing-closed-prs"),
    # `pr` ANYWHERE in argv used to fire: `gh_pr_subcommand` scans for the token
    # rather than requiring it to be the group, which is the safe direction for
    # the fail-closed gate it was written for and the wrong one here.
    pytest.param("gh run list --workflow pr close", id="pr-as-a-workflow-name"),
    pytest.param("gh label create pr close", id="pr-as-a-label-name"),
    pytest.param("gh alias set prc -- pr close", id="pr-inside-an-alias-definition"),
    pytest.param("gh config set pr close", id="pr-as-a-config-key"),
    # The trigger words inside an ordinary field VALUE. Self-referential and
    # real: a session commenting on this feature through `gh api` tripped its
    # own advisory.
    pytest.param(
        "gh api repos/o/r/issues/5/comments " + _F + " body='use closePullRequest here'",
        id="the-mutation-named-in-a-comment-body",
    ),
    pytest.param(
        "gh api repos/o/r/pulls/5 -X PATCH " + _F + " title='fix: state=closed parsing'",
        id="the-field-named-in-a-pr-title",
    ),
    pytest.param(
        "gh api graphql " + _F + " query='query { __type(name:\"closePullRequest\") { name } }'",
        id="introspecting-the-mutation-is-not-calling-it",
    ),
    # Scoping to `gh api`: without it, any gh subcommand carrying the word in a
    # body would fire. A mutation deleting that requirement survived the suite.
    pytest.param(
        "gh pr comment 5 --body 'we could use closePullRequest'",
        id="a-pr-comment-mentioning-the-mutation",
    ),
    # A PATCH to a SUB-RESOURCE carrying a real state field. This is the case
    # where the endpoint `fullmatch` is load-bearing: the query-string cases
    # above are already silent because no TOKEN is a state field, so they never
    # exercised it and stayed green with the lookahead reverted.
    pytest.param(
        "gh api repos/o/r/pulls/5/reviews -X PATCH " + _F + " state=closed",
        id="patching-a-sub-resource-is-not-closing-the-pr",
    ),
    # A non-`api` subcommand carrying a real mutation in a field value. This is
    # where the `gh api` scoping is load-bearing; the comment-body case above is
    # not, because it has no `query=` for the mutation pattern to match.
    pytest.param(
        "gh workflow run x.yml "
        + _F
        + " query='mutation { closePullRequest(input:{x:1}) { id } }'",
        id="a-workflow-input-that-happens-to-carry-a-mutation",
    ),
]


@pytest.mark.parametrize("command", FIRES)
def test_it_fires_on_a_close_it_can_see(command):
    r = _run(command)
    assert r.returncode == 0
    assert "closes a pull request" in _note(r)


@pytest.mark.parametrize("command", SILENT)
def test_it_stays_silent_on_everything_else(command):
    """Noise is the failure mode that gets an advisory ignored, and an advisory
    nobody reads is indistinguishable from one that never fired."""
    r = _run(command)
    assert r.returncode == 0
    assert _note(r) == ""


def test_the_note_states_the_limit_it_cannot_see():
    """The boundary is printed where it is RELIED ON, not only in a docstring.

    The forms this hook cannot read — a mutation on stdin or from a file — are
    exactly the ones a reader would otherwise assume were covered. Saying so in
    the note is what makes silence honest rather than misleading.
    """
    note = _note(_run("gh pr close 1"))
    assert "--input -" in note
    assert "query=@file" in note
    assert "silence is not evidence" in note


def test_it_names_every_distinct_close_in_a_compound():
    """A reader deciding whether to proceed needs to know the command closes two
    things by two routes, not that it closes something."""
    note = _note(
        _run(
            "gh pr close 1 && gh api repos/o/r/pulls/5 -X PATCH " + _F + " state=closed",
        )
    )
    assert "`gh pr close`" in note
    assert "REST" in note


# --------------------------------------------------------------- it must never
# block, prompt, or break the command


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty-payload"),
        pytest.param({"tool_input": {}}, id="no-command"),
        pytest.param({"tool_input": {"command": ""}}, id="empty-command"),
        pytest.param({"tool_input": {"command": "gh pr close 'unterminated"}}, id="unparseable"),
        pytest.param({"tool_input": None}, id="null-tool-input"),
        pytest.param({"tool_input": {"command": 42}}, id="non-string-command"),
    ],
)
def test_a_malformed_payload_never_breaks_the_command(payload):
    """Fail OPEN. This runs on every Bash call, so a crash here would be a
    session-wide outage caused by a hook that enforces nothing."""
    r = _run("", payload=payload)
    assert r.returncode == 0


def test_it_never_emits_a_permission_decision():
    """The whole design rests on this.

    An enforcement version of this hook drew eleven review findings, five of
    them one unanswerable question: can a gate tell from argv whether a command
    closes a PR? It cannot — `gh api graphql` takes its body from stdin or a
    file. Under enforcement that gap is a bypass that must be closed and cannot
    be; under advisory it is a note that did not fire. If a `permissionDecision`
    ever appears here, that trade is silently reversed.
    """
    out = _run("gh pr close 1").stdout
    assert "permissionDecision" not in out
    assert json.loads(out)["hookSpecificOutput"].keys() == {
        "hookEventName",
        "additionalContext",
    }


def test_a_dispatched_session_is_advised_exactly_like_a_foreground_one():
    """The standing axiom: a background session must stay as capable as a
    foreground one, and an ask with no human present is a block nobody
    intended. Identical output is how that is held here — there is no
    session-type branch to drift."""
    fg = _run("gh pr close 1", payload={"tool_input": {"command": "gh pr close 1"}})
    # GENESIS_CC_SESSION is an ENVIRONMENT variable, which is how every other
    # consumer in this repo detects a dispatched session. The first version of
    # this test passed `genesis_cc_session` as a PAYLOAD key -- a name nothing
    # anywhere reads -- so it compared two identical payloads and the property
    # in the docstring was untested. The hook reads no environment today, and
    # that is the point: this fails the day someone adds the branch.
    bg = _run("gh pr close 1", env={"GENESIS_CC_SESSION": "1"})
    assert fg.returncode == bg.returncode == 0
    assert _note(fg) == _note(bg) != ""


# ------------------------------------------------------------- the pieces
# the end-to-end cases cannot pin


def _module():
    """Import the hook directly, for the predicates that have no behavioural
    signature through the CLI."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_pr_close_advisory", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("gh pr close 1", True),
        ("/usr/bin/gh pr close 1", True),  # a path separator must not block it
        ("echo through the high road", False),
        ("high-gh-road", False),
        ("weigh the options", False),
    ],
)
def test_the_prefilter_pattern_itself(text, hit):
    """The prefilter never changes an outcome, so no end-to-end case can test
    it. Deleting it leaves the whole suite green — which is precisely why the
    pattern is pinned here directly."""
    assert bool(_module()._GH_WORD.search(text)) is hit


@pytest.mark.parametrize(
    ("argv", "group"),
    [
        (["gh", "pr", "close", "1"], "pr"),
        (["gh", "--repo", "owner/repo", "pr", "close"], "pr"),
        (["gh", "-R", "owner/repo", "pr", "close"], "pr"),
        (["gh", "--repo=owner/repo", "pr", "close"], "pr"),
        (["gh", "run", "list", "--workflow", "pr", "close"], "run"),
        (["gh"], None),
        (["gh", "--repo"], None),  # a value flag with nothing after it
    ],
)
def test_the_group_word_reads_the_option_table_not_the_dashes(argv, group):
    """A hand-rolled "first token without a dash" returned `owner/repo` for
    `gh --repo owner/repo pr close` and silenced a real close — the same defect
    the shared parser was hardened against, reproduced one level up. The table
    comes off `shell_parse`'s spec so the two cannot disagree."""
    assert _module()._gh_group(argv) == group


def _fires(command: str) -> bool:
    """Does the detector report a close for this single command?

    Unit-level via `_module()`, the pattern this file already uses for
    predicates whose behavioural signature through the CLI would be identical
    across the cases being distinguished. Single commands only -- the compound
    handling has its own cases in the smoke matrix.
    """
    import shlex

    return _module()._closes_a_pr(shlex.split(command)) is not None


@pytest.mark.parametrize(
    ("command", "fires"),
    [
        # The forms the field may legitimately take. These pin the SAME
        # property the deleted `_is_state_closed_field` unit test pinned --
        # that only a real `state` field counts -- but through the detector
        # rather than through a helper, so the assertion survives the next
        # change of implementation rather than naming one.
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH " + _F + " state=closed",
            True,
            id="separated-field",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH " + _F + "state=closed",
            True,
            id="glued-short-field",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH --field=state=closed",
            True,
            id="attached-long-field",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 --method patch " + _F + " state=closed",
            True,
            id="method-name-is-case-insensitive",
        ),
        # A token that merely ENDS in the phrase is not the field. The old
        # suffix test accepted any dashed token, so an output template read as
        # a close while the request updated only a title.
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH " + _F + " title=x --template=state=closed",
            False,
            id="an-output-template-is-not-a-field",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH " + _F + " notstate=closed",
            False,
            id="a-field-whose-name-merely-ends-in-state",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH " + _F + " title='fix: state=closed parsing'",
            False,
            id="a-title-that-mentions-the-phrase",
        ),
    ],
)
def test_only_a_real_state_field_counts(command, fires):
    """Joined-argv matching failed in BOTH directions here: a word boundary
    missed the glued flag, and widening it swallowed a PR title that mentions
    the phrase. Only a parsed FIELD knows whether what precedes `state` is a
    flag or a word, and only its KEY says whether the field is `state` at all.
    """
    assert _fires(command) is fires


@pytest.mark.parametrize(
    ("command", "fires"),
    [
        # MEASURED from `gh api --help` (gh 2.101.0): the method defaults to
        # GET, and to POST as soon as any parameter is added -- never to
        # PATCH. So an endpoint-plus-field command with no method states a
        # request that closes nothing, and reading the verdict off the path
        # and the field alone reported it as a close.
        pytest.param(
            "gh api repos/o/r/pulls/5 " + _F + " state=closed",
            False,
            id="no-method-is-a-POST-and-closes-nothing",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X GET " + _F + " state=closed",
            False,
            id="an-explicit-GET-reads",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH " + _F + " state=closed",
            True,
            id="an-explicit-PATCH-closes",
        ),
        # `-i` is a value flag under `pr checks --interval` but the valueless
        # `--include` under `api`. A union-only post-path scan consumed `-X`
        # as `-i`'s value and lost the PATCH entirely (Devin Review, #2256).
        pytest.param(
            "gh api repos/o/r/pulls/5 -i -X PATCH " + _F + " state=closed",
            True,
            id="include-flag-does-not-eat-the-method",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -i -X PATCH",
            False,
            id="include-flag-no-field-is-still-a-read",
        ),
    ],
)
def test_the_request_method_decides_whether_anything_is_written(command, fires):
    """A close is a WRITE, and gh will not make one unless it is asked to."""
    assert _fires(command) is fires


@pytest.mark.parametrize(
    ("command", "fires"),
    [
        # `api` has to be the GROUP. Token membership also sees workflow
        # names, alias bodies and ordinary operands.
        pytest.param(
            "gh workflow run api " + _F + " query='mutation { closePullRequest(x) }'",
            False,
            id="a-workflow-named-api",
        ),
        pytest.param(
            "gh api graphql " + _F + " query='mutation { closePullRequest(x) }'",
            True,
            id="the-real-graphql-close",
        ),
        # The mutation must live in the `query` field. The joined-argv regex
        # could begin inside one field's value and end inside another's, so a
        # query that creates an issue, beside an unrelated field naming the
        # mutation, reported a close.
        pytest.param(
            "gh api graphql "
            + _F
            + " query='mutation { createIssue(x) }' "
            + _F
            + " body=closePullRequest",
            False,
            id="the-mutation-name-in-a-neighbouring-field",
        ),
        pytest.param(
            "gh api repos/o/r/issues/5/comments "
            + _F
            + " body='example: query=mutation { closePullRequest(x) }'",
            False,
            id="a-comment-quoting-a-sample-query",
        ),
        # An endpoint that appears only as field DATA is not the endpoint.
        pytest.param(
            "gh api graphql -X PATCH " + _F + " body=repos/o/r/pulls/5 " + _F + " state=closed",
            False,
            id="a-path-carried-as-prose",
        ),
    ],
)
def test_the_api_group_and_the_field_key_are_read_by_position(command, fires):
    """Every one of these fired under joined-argv matching, and they are ONE
    class: the detector was reading TEXT where it had STRUCTURE available."""
    assert _fires(command) is fires


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("gh pr close 1 --help", id="help-after-the-operand"),
        pytest.param("gh pr close 1 -h", id="short-help"),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH " + _F + " state=closed --help",
            id="api-help",
        ),
        # `-i` is the valueless `--include` under `api`; a union-only scan
        # would consume this `--help` as `-i`'s value and let a help-only
        # command look like a close (same defect as the `-X` swallow).
        pytest.param(
            "gh api repos/o/r/pulls/5 -i --help -X PATCH " + _F + " state=closed",
            id="help-after-valueless-include",
        ),
    ],
)
def test_a_help_invocation_performs_nothing(command):
    """MEASURED: `gh api repos/octocat/hello-world --help` prints help and
    issues no request even with the endpoint already given, so a terminal help
    flag means there is nothing to note. An advisory that fires on `--help` is
    teaching its reader to ignore it."""
    assert _fires(command) is False


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "gh pr close 1 --comment '--help'",
            id="a-closing-comment-whose-text-is-a-help-flag",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X PATCH " + _F + " state=closed " + _F + " body='--help'",
            id="a-field-value-that-is-a-help-flag",
        ),
    ],
)
def test_help_INSIDE_a_value_does_not_silence_a_real_close(command):
    """The guard-the-guard for the test above, and it caught a real defect in
    the first version of that fix.

    A bare `"--help" in argv` scan reads a VALUE as a flag, so it would go
    silent on a command that really closes -- turning a false-positive fix
    into a false NEGATIVE, which is the worse direction. The `--comment` case
    is the one that bit: `gh pr close -c/--comment` takes a value (MEASURED
    from its own help) and the shared spec knows only `-R`, so that flag had
    to be named for the skip to happen."""
    assert _fires(command) is True


@pytest.mark.parametrize(
    ("command", "fires"),
    [
        # Each of these is a REAL close that the first structured version
        # missed — false NEGATIVES, where round 1's were false positives.
        pytest.param(
            "gh api /repos/o/r/pulls/5 -X PATCH " + _F + " state=closed",
            True,
            id="leading-slash-endpoint",
        ),
        pytest.param("gh pr -c note close 1", True, id="close-flag-before-the-subcommand"),
        pytest.param(
            "gh -X PATCH api repos/o/r/pulls/5 " + _F + " state=closed",
            True,
            id="api-flag-before-the-group",
        ),
        pytest.param(
            "gh api repos/o/r/pulls/5 -X=PATCH " + _F + " state=closed",
            True,
            id="equals-in-a-shorthand-value",
        ),
        # The widened skip must not start EATING positionals it should see.
        # These are the direction the widening could break, and they are the
        # reason it is a measured union rather than "skip anything dashed".
        pytest.param("gh pr list", False, id="still-silent-on-a-list"),
        pytest.param("gh -R o/r pr list", False, id="still-silent-with-a-repo-flag"),
        pytest.param(
            "gh run list --workflow pr close", False, id="still-silent-on-a-workflow-named-pr"
        ),
        pytest.param("gh pr create -" + "f", False, id="a-valueless-short-under-another-group"),
    ],
)
def test_gh_accepts_group_flags_out_of_order_and_so_must_this(command, fires):
    """MEASURED against the real CLI, which is the only authority here.

    `gh -X PATCH api --help` and `gh -c note pr close --help` both resolve,
    and `gh -f pr create --help` FAILS with `unknown command "create"` — gh
    having eaten `pr` as the value of `-f`. So gh steps over subcommand-local
    value flags wherever they appear, and a parser that does not mislocates
    the group exactly where gh does not. The union is a reading of gh's
    behaviour, not a guess about it.
    """
    assert _fires(command) is fires


def test_a_compound_close_reports_HOW_MANY_not_how_many_mechanisms():
    """Deduplicating by mechanism erased repetition.

    Two closes spelled the same way collapse to one reason, and a lead that
    took its number from the reason LIST then described a compound retirement
    of two PRs as though it touched one -- understating the blast radius in
    exactly the situation the rule exists for. Occurrences and distinct
    mechanisms are now counted separately.
    """
    note = _note(_run("gh pr close 1 && gh pr close 2"))
    assert "has 2 steps that close a pull request" in note, note


def test_one_close_still_reads_as_one():
    """The guard-the-guard for the count: a change that made every note plural
    would satisfy the test above perfectly."""
    note = _note(_run("gh pr close 1"))
    assert "closes a pull request," in note, note


def test_the_issues_endpoint_does_not_assert_a_pull_request():
    """`/issues/N` addresses ordinary issues AND pull requests, and the command
    text says nowhere which this number is.

    The standing rule being surfaced is about PRs, so asserting one here would
    be the advisory inventing the fact that makes it relevant. Detection is
    kept -- closing a PR through the issues path is documented and real -- and
    the WORDING is what carries the uncertainty.
    """
    note = _note(_run("gh api repos/o/r/issues/5 -X PATCH " + _F + " state=closed"))
    assert note, "the issues path should still be detected"
    assert "pull request or issue" in note, note


def test_the_pulls_endpoint_is_still_unambiguous():
    """The other direction: the hedge must not leak onto the path that IS
    unambiguous, or every note starts hedging and the distinction is lost."""
    note = _note(_run("gh api repos/o/r/pulls/5 -X PATCH " + _F + " state=closed"))
    assert "pull request or issue" not in note, note
    assert "closes a pull request," in note, note


@pytest.mark.parametrize(
    ("command", "fires"),
    [
        pytest.param(
            "cat >> /tmp/notes.md <<'MDEOF'\nsee `gh pr close 1`\nMDEOF",
            False,
            id="prose-in-a-quoted-heredoc",
        ),
        pytest.param(
            "out=$(gh pr list --json number)", False, id="a-substitution-that-closes-nothing"
        ),
        pytest.param("gh pr close 1", True, id="the-real-thing-still-fires"),
    ],
)
def test_text_NESTED_inside_a_quoted_heredoc_is_prose_not_a_command(command, fires):
    """A quoted heredoc delimiter suppresses expansion in bash, so the body is
    text the shell never runs. `shell_parse` parses it anyway — the right
    fail-closed posture for a destructive guard, the wrong one for an advisory
    whose stated fatal failure is noise.

    MEASURED over 57,445 unique real Bash commands: 36 fired and 4 were false
    positives, every one of them prose ABOUT closing a PR inside a heredoc or
    a substitution — three of them written while developing this very hook.
    Skipping depth>0 removed 4 of 4 and lost 0 of 32 true positives.

    The cost is real and stated in the code: a genuine `bash -c 'gh pr close
    1'` is also depth 1 and is now missed. That trades a measured noise class
    for an unmeasured coverage one, which is the right direction here and is
    named in the advisory's own limit text.
    """
    note = _note(_run(command))
    assert bool(note) is fires, note


def test_the_hook_is_actually_wired():
    """The entry is inert in a worktree (the launcher resolves hooks from the
    main checkout), so nothing else in this file would notice a merge that
    landed the script and dropped the wiring."""
    settings = json.loads((_HOOK.parents[2] / ".claude" / "settings.json").read_text())
    commands = [
        h.get("command", "")
        for entry in settings["hooks"]["PreToolUse"]
        for h in entry.get("hooks", [])
    ]
    assert any("hooks/pr_close_advisory.py" in c for c in commands)


def test_an_untokenizable_command_is_still_advised_on():
    """Pins the blind-parse decision, which a comment used to assert and the
    code did not implement.

    `analyze_checked` reports a blind spot for this input AND returns a
    segment. Silencing on the flag would lose a real close attempt to buy
    nothing, since the other blind spot (a parse bound) returns no segments and
    is therefore already silent.
    """
    assert "closes a pull request" in _note(_run("gh pr close 1 #'oops"))
