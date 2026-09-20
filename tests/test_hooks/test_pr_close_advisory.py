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
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "pr_close_advisory.py"

#: Assembled rather than written literally: a bare `-f` in a test source line
#: reads as a force-push to the repo's own shell guard, which blocks the whole
#: command it appears in.
_F = "-" + "f"


def _run(command: str, payload: dict | None = None) -> subprocess.CompletedProcess:
    body = payload if payload is not None else {"tool_input": {"command": command}}
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=60,
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
    # where the `(?![/\w])` boundary is load-bearing: the query-string cases
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
    bg = _run(
        "gh pr close 1",
        payload={"tool_input": {"command": "gh pr close 1"}, "genesis_cc_session": True},
    )
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


@pytest.mark.parametrize(
    ("token", "is_field"),
    [
        ("state=closed", True),
        ("-fstate=closed", True),
        ("--field=state=closed", True),
        ("mystate=closed", False),
        ("repos/o/r/pulls/5?state=closed", False),  # a GET filter reads, not closes
        ("title=fix: state=closed parsing", False),
    ],
)
def test_the_state_field_is_judged_per_token(token, is_field):
    """Joined-argv matching failed in BOTH directions here: `\b` missed the
    glued flag, and widening it swallowed a PR title that mentions the phrase.
    Only the token knows whether what precedes `state` is a flag or a word."""
    assert _module()._is_state_closed_field(token) is is_field


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
