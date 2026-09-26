"""A line continuation splits the parse where the shell joins — say so.

THE DEFECT THIS LOCKS. ``parse_segments`` treated the newline after an unquoted
backslash as a command separator. The shell does the opposite: it REMOVES the pair
and runs the two lines as one command. A gated verb on the far side of a continuation
therefore landed in a segment of its own, no guard found the operation it was looking
for, and "not found" was read as "not present". The scanner still splits there, and
now RECORDS that it did (``Segment.cont_split``, and ``cont_inner`` for a
continuation inside double quotes, where no split happens but the shell still joins
the word).

MEASURED against the real guards, each driven as a subprocess, with the one-line form
as the control and a shim on PATH recording what bash actually executes: four guards —
the protected-path net, the discard guard, the push/merge gate and the commit gate —
refused the plain command and ALLOWED the continued one.

WHY A CONTINUATION IS REPORTED RATHER THAN JOINED. Joining the halves is what the
shell does, but a join made where the shell does NOT continue a line — inside a `#`
comment, which the shell ends at the newline — deletes a real separator and makes the
next command vanish from every consumer. An earlier attempt at the join drew exactly
that fail-open in two consecutive review rounds. Splitting is wrong in a direction that
costs a rewrite; joining is wrong in a direction that costs the thing the guard
protects. So the split stays and the caller is TOLD.

EVERY OTHER ESCAPE IS HONOURED. `\\;`, `\\|`, `\\&`, `\"` and the rest are literal
characters to the shell, and the scanner now keeps each pair whole instead of
dispatching the escapee — an escaped quote used to open a quoted run that hid every
later command. Only backslash-newline is left unconsumed, for the reason above.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))

import shell_parse as sp  # noqa: E402

# Built rather than written literally, so no editor or formatter can quietly normalise
# the one byte sequence these tests are about.
_CONT = " \\\n  "


def _cause(command: str) -> str | None:
    _segs, blind = sp.analyze_checked(command)
    return None if blind is None else blind.cause


def test_a_continuation_before_the_verb_is_reported():
    """The shape the guards were measured leaking on."""
    assert _cause("git" + _CONT + "clean -fd") == sp._BLIND_CONTINUATION.cause


def test_the_same_command_on_one_line_is_not_reported():
    """CONTROL. Without it, a function that reported EVERY command would pass above."""
    assert _cause("git clean -fd") is None


def test_an_even_length_backslash_run_is_not_reported():
    """PARITY, and it is the whole correctness of the predicate.

    In an even-length run each backslash is escaped by its neighbour, so the last one
    is a literal character and the newline after it really does separate two commands —
    the split was RIGHT and reporting it would be a pure over-block. The sibling guard
    learned the same case-split in the opposite direction: folding an even run deleted
    a real separator, glued the next command's first word onto the previous token, and
    ALLOWED a destructive command.
    """
    assert _cause("echo a \\\\\n  rm -rf /tmp/x") is None


def test_a_continuation_inside_single_quotes_is_not_reported():
    """Inside single quotes the shell keeps a backslash-newline literally."""
    assert _cause("echo 'a \\\n  b'") is None


def test_a_continuation_inside_double_quotes_is_reported():
    """Inside double quotes the shell REMOVES a backslash-newline and joins the word.

    No split happens there, so a predicate reading a trailing backslash off the
    segment never saw it — and MEASURED on main, a force flag spelled across one
    inside quotes asked as an ordinary push while bash force-pushed. The scanner now
    records the join itself, and the group text reads the joined word.
    """
    command = 'git push origin main "--for\\\nce"'
    assert _cause(command) == sp._BLIND_CONTINUATION.cause
    segs, _ = sp.analyze_checked(command)
    (group,) = sp.continuation_groups(segs)
    assert "--" + "force" in sp.group_text(group)


def test_a_command_that_is_also_untokenizable_keeps_the_older_cause():
    """PRECEDENCE, tested where it can actually be wrong: both causes at once.

    This cause is reported LAST, after every cause that existed before it. That is a
    property rather than a ranking, and it is the one that makes the change safe to
    measure: every command that already had a blind spot keeps the exact cause and
    hint it had, so the rule can only ADD coverage and can never reword or re-rank an
    existing refusal. That holds by construction — the cause is checked after every
    earlier return — and this cell is what would catch the construction changing.

    A single-cause cell cannot see a precedence bug — each cause alone was already
    correct. It exists only in the intersection.
    """
    both = "git" + _CONT + "clean -fd  # don't"
    assert _cause(both) == sp._BLIND_UNTOKENIZABLE.cause
    # Control: the same command without the untokenizable half reports the new cause,
    # so the assertion above is precedence and not "this shape is never reported".
    assert _cause("git" + _CONT + "clean -fd") == sp._BLIND_CONTINUATION.cause


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'git" + _CONT + "clean -fdx'",
        'echo "$(git' + _CONT + 'clean -fdx)"',
    ],
    ids=["bash-c", "command-substitution"],
)
def test_a_continuation_inside_a_nested_script_is_reported(command):
    """A nested script is split by the same scanner and joined by the same shell.

    An earlier version skipped nested segments on the theory that they end because
    their substitution closed. That was false, and every nested continuation went
    unreported. MEASURED: the one-line nested form was refused while the continued one
    was allowed.
    """
    segs, _blind = sp.analyze_checked(command)
    assert any(s.depth for s in segs), "fixture must produce a nested segment"
    assert _cause(command) == sp._BLIND_CONTINUATION.cause


def test_an_escaped_backtick_in_double_quotes_is_not_a_nested_script():
    r"""`\`word\`` inside double quotes is literal text, not a substitution.

    Read as one, it carved out a bogus nested script ending in a backslash — the
    source of almost every nested report before this change (MEASURED 371 commands
    down to 8 over the recorded corpus).
    """
    command = 'grep -n "a stale \\`.pyc\\` file" notes.md'
    segs, _blind = sp.analyze_checked(command)
    assert not any(s.depth for s in segs), segs
    assert _cause(command) is None


def test_a_real_substitution_after_escaped_backticks_is_still_surfaced():
    """An escaped backtick must not be read as an OPENER either.

    Read as one, it paired with the real backtick that follows and swallowed it, so
    the real substitution after it was never extracted: a hidden command, which is
    the fail-open direction.
    """
    segs, _blind = sp.analyze_checked('echo "\\`a\\` `date`"')
    assert any(s.argv == ["date"] and s.depth >= 1 for s in segs), segs


def test_an_escaped_dollar_paren_is_not_a_substitution():
    """`\\$(x)` is literal text to the shell, so no nested command runs."""
    segs, _blind = sp.analyze_checked("echo \\$(touch marker)")
    assert not any(s.depth for s in segs), segs


def test_a_backtick_substitution_nested_in_a_backtick_substitution_is_surfaced():
    """The shell closes old-style substitution at the first UNESCAPED backtick and
    removes the backslash before an inner one, so the inner command runs too."""
    segs, _blind = sp.analyze_checked("echo `echo \\`date\\``")
    assert any(s.argv == ["date"] and s.depth >= 1 for s in segs), segs


@pytest.mark.parametrize(
    ("command", "expect_argv"),
    [
        (
            "find . -name x -exec grep -l push {} \\;",
            ["find", ".", "-name", "x", "-exec", "grep", "-l", "push", "{}", ";"],
        ),
        (
            "gh api repos/o/r/commits?sha=main\\&per_page=6",
            ["gh", "api", "repos/o/r/commits?sha=main&per_page=6"],
        ),
    ],
    ids=["escaped-semicolon", "escaped-ampersand"],
)
def test_an_escaped_separator_is_a_literal_argument_not_a_split(command, expect_argv):
    """The shell keeps a backslash-escaped separator as a literal inside ONE command.

    It used to split there and be reported with the continuation cause, whose remedy
    ("put it on one line") could not be performed on a command already on one line.
    Now the scanner keeps the pair, the argv matches what the shell passes, and there
    is nothing to report.
    """
    segs, blind = sp.analyze_checked(command)
    assert [s.argv for s in segs] == [expect_argv]
    assert blind is None


def test_an_escaped_quote_does_not_hide_the_commands_after_it():
    """An escaped quote opens no quoted run.

    It used to, and the run lasted to end of string: the whole line parsed as ONE
    `echo` segment with no blind spot, so a gated command after it was invisible to
    every guard — a silent allow on main, measured through the real guards.
    """
    segs, blind = sp.analyze_checked('echo \\"x; git status; \\"')
    assert ["git", "status"] in [s.argv for s in segs], segs
    assert blind is None


def test_the_cause_describes_a_continuation_only():
    """No other escape reaches this cause any more, so the text says what it is."""
    assert "`;`" not in sp._BLIND_CONTINUATION.cause
    assert "backslash" in sp._BLIND_CONTINUATION.cause
    assert "one line" in sp._BLIND_CONTINUATION.hint


def test_a_lone_backslash_after_an_operator_is_not_reported():
    """`a && <continuation> b` leaves a segment that is only a backslash, and the
    shell's join gives the same two commands the split did. MEASURED: this shape was
    turning an ordinary push from ASK into BLOCK."""
    assert _cause("git add x &&" + _CONT + "git status") is None


def test_the_blind_spot_is_registered_in_the_domain():
    """`_ALL_BLIND_SPOTS` is what the domain-wide invariant test iterates.

    A cause missing from it is invisible to that check — which is exactly the drift
    the tuple was introduced to end, so the registration is asserted rather than
    assumed.
    """
    assert sp._BLIND_CONTINUATION in sp._ALL_BLIND_SPOTS


# ---------------------------------------------------------------------------
# End-to-end: the real guards, driven as subprocesses. The unit assertions above
# pin the PARSER's report; only this pins that the report changes a VERDICT, which
# is the whole point of the change and is not derivable from the parse.
# ---------------------------------------------------------------------------

_GUARD_CASES = [
    ("git_discard_guard", "hooks/git_discard_guard.py", "git clean -fd", "git status"),
    (
        "git_push_guard",
        "hooks/git_push_guard.py",
        " ".join(["git", "push", "origin", "main", "--" + "force"]),
        "git log --oneline -5",
    ),
]


def _run(rel: str, command: str, home: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / rel)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={**os.environ, "HOME": str(home)},
        timeout=120,
    )


@pytest.mark.parametrize(("guard", "rel", "gated", "benign"), _GUARD_CASES)
def test_a_continued_gated_command_is_refused_like_the_one_line_form(
    tmp_path, guard, rel, gated, benign
):
    """The acceptance bar: replay the measured bypass through the real guard.

    Three cells, because two of them are what make the third mean anything. The
    one-line form is the positive control — if it does not refuse, the fixture is not
    reaching the gate and the continued cell proves nothing. The benign form is the
    negative control — a guard that refused everything would pass the first two cells
    perfectly while wedging the session.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    continued = gated.replace(" ", _CONT, 1)

    plain = _run(rel, gated, home, _REPO_ROOT)
    assert plain.returncode == 2, (
        f"{guard} did not refuse the one-line form, so this fixture never reaches the "
        f"gate and the continued cell below would prove nothing.\n{plain.stderr[:300]}"
    )
    cont = _run(rel, continued, home, _REPO_ROOT)
    assert cont.returncode == 2, (
        f"{guard} allowed the continued form of a command it refuses on one line — the "
        f"bypass this change exists to close.\n{cont.stderr[:300]}"
    )
    ok = _run(rel, benign, home, _REPO_ROOT)
    assert ok.returncode == 0, (
        f"{guard} refused a benign command; a guard that refuses everything satisfies "
        f"the cells above without protecting anything.\n{ok.stderr[:300]}"
    )


def test_the_one_line_dry_run_is_still_allowed(tmp_path):
    """The exact dry run on one line is untouched by this change.

    Deliberately NOT labelled as proof that the discard guard refuses on a
    continuation rather than on blindness in general. That narrowing is pinned by
    `test_git_discard_guard.py::test_an_untokenizable_clean_mentioning_command_is_still_ALLOWED`,
    which a `blind is not None` widening turns red — this cell cannot, because `git clean
    -n` has no blind spot at all. An earlier version claimed that job and could not do it.
    """
    home = tmp_path / "home_dry"
    home.mkdir(parents=True, exist_ok=True)
    res = _run("hooks/git_discard_guard.py", "git clean -n", home, _REPO_ROOT)
    assert res.returncode == 0, res.stderr[:300]


def test_a_benign_dry_run_does_not_shield_a_continued_clean(tmp_path):
    """THE DECOY: a judged, allowed segment must not stand the continuation net down.

    An earlier cut refused only when no clean segment had been judged. In the command
    below the dry run IS judged and allowed, so that conjunct went false and the real
    `git clean -fd` the shell runs after it was never examined — measured allowed,
    with bash running both. The same command on one line was always refused, which is
    the control that makes the continued cell mean something.
    """
    home = tmp_path / "home_decoy"
    home.mkdir(parents=True, exist_ok=True)
    rel = "hooks/git_discard_guard.py"
    one_line = _run(rel, "git clean -n && git clean -fd", home, _REPO_ROOT)
    assert one_line.returncode == 2, one_line.stderr[:300]
    decoy = _run(rel, "git clean -n && git" + _CONT + "clean -fd", home, _REPO_ROOT)
    assert decoy.returncode == 2, (
        f"a benign dry run shielded a continued `git clean -fd`.\n{decoy.stderr[:300]}"
    )


def test_the_override_still_waives_a_continued_clean(tmp_path):
    """The documented escape works on the continued form, as it does on one line.

    Without this the continuation refusal would be the only clean refusal with no
    override at all, which turns a rewrite-cost refusal into a wall.
    """
    home = tmp_path / "home_override"
    home.mkdir(parents=True, exist_ok=True)
    rel = "hooks/git_discard_guard.py"
    refused = _run(rel, "git" + _CONT + "clean -fd", home, _REPO_ROOT)
    assert refused.returncode == 2, refused.stderr[:300]
    waived = _run(rel, "git" + _CONT + "clean -fd  # discard-override", home, _REPO_ROOT)
    assert waived.returncode == 0, waived.stderr[:300]


def test_the_protected_path_net_refuses_a_continued_removal(tmp_path):
    """The fourth guard the bypass was measured on, with its own positive control."""
    home = tmp_path / "home_prot"
    protected = home / "genesis" / "data"
    protected.mkdir(parents=True)
    rel = "hooks/protected_paths_guard.py"
    plain = _run(rel, f"rm -rf {protected}", home, _REPO_ROOT)
    assert plain.returncode == 2, (
        f"the one-line removal was not refused, so the fixture never reached the "
        f"protected-path check.\n{plain.stderr[:300]}"
    )
    cont = _run(rel, "rm" + _CONT + f"-rf {protected}", home, _REPO_ROOT)
    assert cont.returncode == 2, cont.stderr[:300]


def test_the_commit_gate_refuses_a_hook_skip_severed_past_the_split(tmp_path):
    """A continuation AFTER the verb, where the commit segment still parses.

    Rule 0 refuses `--no-verify` by reading the commit segment's argv, and a split
    between `-m` and its message moves the flag into a segment with no `git` in it.
    MEASURED before this change: the gate then ASKED as for an ordinary commit while
    bash joined the lines and skipped every hook. The one-line form is the control.
    """
    home = tmp_path / "home_noverify"
    home.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo_noverify"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature-x", str(repo)], check=True)
    rel = "review_enforcement_commit.py"
    plain = _run(rel, "git commit -m x --no-verify", home, repo)
    assert plain.returncode == 2, plain.stderr[:300]
    severed = _run(rel, "git commit -m" + _CONT + "x --no-verify", home, repo)
    assert severed.returncode == 2, severed.stdout[:300] + severed.stderr[:300]
    assert "full argument list" in severed.stderr, severed.stderr[:300]


def test_the_commit_gate_refuses_a_continued_commit_and_allows_a_benign_backslash(tmp_path):
    """The commit gate, plus a negative control on the axis the change touches.

    A benign control with NO backslash cannot detect an over-block by this predicate,
    which is what an earlier version of the cells above used. This one carries an odd
    backslash run AND names a gated word, so it fails if escaped characters go back to
    being split and reported.
    """
    home = tmp_path / "home_commit"
    home.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature-x", str(repo)], check=True)
    rel = "review_enforcement_commit.py"
    cont = _run(rel, "git" + _CONT + 'commit -m "x"', home, repo)
    assert cont.returncode == 2, cont.stderr[:300]
    # Names a gated word AND carries an odd backslash run that is not a continuation:
    # an escaped separator, which the old scanner split and reported.
    benign = "find . -name x -exec echo commit {} \\;"
    assert _cause(benign) is None
    ok = _run(rel, benign, home, repo)
    assert ok.returncode == 0, ok.stderr[:300]


# ---------------------------------------------------------------------------
# End-to-end cells for the review's findings. Each pairs the continued or escaped
# form with the one-line form's verdict, because the bar is "the same verdict the
# shell's own reading of the command would get", not "refused".
# ---------------------------------------------------------------------------


def _decision(res: subprocess.CompletedProcess) -> str:
    try:
        out = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    d = out.get("hookSpecificOutput", {}).get("permissionDecision", "")
    if d:
        return d
    return (
        "block"
        if res.returncode == 2
        else ("allow" if res.returncode == 0 else f"rc{res.returncode}")
    )


_FORCE = "--" + "force"


def test_an_escaped_quote_no_longer_hides_a_gated_push(tmp_path):
    home = tmp_path / "home_escq"
    home.mkdir(parents=True, exist_ok=True)
    rel = "hooks/git_push_guard.py"
    plain = _run(rel, f"git push origin main {_FORCE}", home, _REPO_ROOT)
    assert _decision(plain) == "block", plain.stderr[:300]
    hidden = _run(rel, f'echo \\"x; git push origin main {_FORCE}; \\"', home, _REPO_ROOT)
    assert _decision(hidden) == "block", hidden.stdout[:300] + hidden.stderr[:300]


@pytest.mark.parametrize(
    ("rel", "command"),
    [
        ("hooks/git_discard_guard.py", "bash -c 'git" + _CONT + "clean -fdx'"),
        ("hooks/git_push_guard.py", 'echo "$(git push origin main' + _CONT + _FORCE + ')"'),
    ],
    ids=["discard-bash-c", "push-substitution"],
)
def test_a_nested_continuation_is_refused(tmp_path, rel, command):
    """MEASURED before this: allowed (discard) and ASKED without naming the force (push),
    while the one-line nested forms were refused."""
    home = tmp_path / "home_nested"
    home.mkdir(parents=True, exist_ok=True)
    res = _run(rel, command, home, _REPO_ROOT)
    assert _decision(res) == "block", res.stdout[:300] + res.stderr[:300]


def test_a_lone_backslash_leaves_a_push_with_its_one_line_verdict(tmp_path):
    home = tmp_path / "home_lone"
    home.mkdir(parents=True, exist_ok=True)
    rel = "hooks/git_push_guard.py"
    one = _run(rel, "git add x && git push -u origin feature-x", home, _REPO_ROOT)
    cont = _run(rel, "git add x &&" + _CONT + "git push -u origin feature-x", home, _REPO_ROOT)
    assert _decision(cont) == _decision(one), (cont.stderr[:300], one.stderr[:300])


def test_a_continued_command_that_only_says_cleanup_is_not_refused(tmp_path):
    """MEASURED: 42 of 42 discard-guard refusals of continued commands in the recorded
    corpus had no `git clean` at all, and each was told it named one."""
    home = tmp_path / "home_cleanup"
    home.mkdir(parents=True, exist_ok=True)
    res = _run(
        "hooks/git_discard_guard.py",
        # "github" matters: without a `git` substring the guard exits at its first
        # early-out and this cell would never reach the branch under test.
        'gh pr create --title "chore: cleanup"' + _CONT + '--body "see github"',
        home,
        _REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr[:300]


def test_the_override_binds_to_the_continued_clean_not_another_command(tmp_path):
    """MEASURED before this: an override on a different, benign command waived the
    continued clean, and one on the continued clean was ignored when a command
    followed it."""
    home = tmp_path / "home_bind"
    home.mkdir(parents=True, exist_ok=True)
    rel = "hooks/git_discard_guard.py"
    elsewhere = _run(
        rel, "git" + _CONT + "clean -fdx && git status  # discard-override", home, _REPO_ROOT
    )
    assert elsewhere.returncode == 2, elsewhere.stderr[:300]
    on_it = _run(rel, "git" + _CONT + "clean -fdx  # discard-override\necho hi", home, _REPO_ROOT)
    assert on_it.returncode == 0, on_it.stderr[:300]


def test_an_escaped_find_terminator_does_not_trip_the_destructive_guard(tmp_path):
    """`-exec rm -f {} \\;` was split at the escaped `;` and refused as unreadable."""
    home = tmp_path / "home_find"
    home.mkdir(parents=True, exist_ok=True)
    res = _run(
        "hooks/destructive_command_guard.py",
        "find . -name '*.pyc' -exec rm -f {} \\;",
        home,
        _REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr[:300]


# ---------------------------------------------------------------------------
# Review round 2. Each cell replays a spelling the second review measured
# getting past a guard, or a false refusal it measured, through the real guard.
# ---------------------------------------------------------------------------

_MID = "\\\n"  # a continuation with nothing around it: splits a WORD


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature-x", str(repo)], check=True)
    return repo


@pytest.mark.parametrize(
    ("rel", "command"),
    [
        ("hooks/git_discard_guard.py", "git cl" + _MID + "ean -fdx"),
        ("hooks/git_discard_guard.py", "gi" + _MID + "t clean -fdx"),
        ("hooks/git_push_guard.py", "git pu" + _MID + "sh origin feature-x"),
        ("hooks/destructive_command_guard.py", "r" + _MID + "m -rf ~"),
    ],
    ids=["discard-verb", "discard-program", "push-verb", "destructive-verb"],
)
def test_a_continuation_inside_the_verb_is_refused(tmp_path, rel, command):
    """B1: every guard tested the RAW text for its verb before parsing, so a
    continuation inside the word hid it and the guard exited early. MEASURED
    allowed on main, with bash running the gated command."""
    home = tmp_path / "home_mid"
    home.mkdir()
    res = _run(rel, command, home, _REPO_ROOT)
    assert _decision(res) == "block", res.stdout[:300] + res.stderr[:300]


def test_a_continuation_inside_the_commit_verb_is_refused(tmp_path):
    home = tmp_path / "home_mid_commit"
    home.mkdir()
    repo = _repo(tmp_path, "repo_mid_commit")
    res = _run(
        "review_enforcement_commit.py", "git com" + _MID + "mit -m x --no-verify", home, repo
    )
    assert _decision(res) == "block", res.stdout[:300] + res.stderr[:300]


def test_a_continuation_inside_the_rm_verb_is_refused_by_the_protected_path_guard(tmp_path):
    home = tmp_path / "home_mid_prot"
    protected = home / "genesis" / "data"
    protected.mkdir(parents=True)
    rel = "hooks/protected_paths_guard.py"
    res = _run(rel, "r" + _MID + f"m -rf {protected}", home, _REPO_ROOT)
    assert res.returncode == 2, res.stderr[:300]
    # The ANCESTOR, which a substring fallback cannot see, across a continuation.
    anc = _run(rel, "rm -rf" + _CONT + f"{home}/genesis", home, _REPO_ROOT)
    assert anc.returncode == 2, anc.stderr[:300]


@pytest.mark.parametrize(
    ("rel", "command", "needs_repo"),
    [
        ("hooks/git_push_guard.py", 'git push origin main "--for' + _MID + 'ce"', False),
        ("review_enforcement_commit.py", 'git commit -m x "--no-ver' + _MID + 'ify"', True),
        ("hooks/git_discard_guard.py", 'git "cl' + _MID + 'ean" -fdx', False),
    ],
    ids=["push-force", "commit-no-verify", "discard-clean"],
)
def test_a_continuation_inside_double_quotes_is_refused(tmp_path, rel, command, needs_repo):
    """S1: inside double quotes the shell joins the word, and no split happened for
    a trailing-backslash test to see. MEASURED on main: the push ASKED without
    naming the force and bash force-pushed; the commit asked as an ordinary one."""
    home = tmp_path / "home_dq"
    home.mkdir()
    cwd = _repo(tmp_path, "repo_dq") if needs_repo else _REPO_ROOT
    res = _run(rel, command, home, cwd)
    assert _decision(res) == "block", res.stdout[:300] + res.stderr[:300]


@pytest.mark.parametrize(
    ("rel", "gated", "needs_repo"),
    [
        ("hooks/git_push_guard.py", "git push origin main " + _FORCE, False),
        ("review_enforcement_commit.py", "git commit -m x --no-verify", True),
        ("hooks/git_discard_guard.py", "git clean -fdx", False),
    ],
    ids=["push", "commit", "discard"],
)
def test_a_comment_line_ending_in_a_backslash_does_not_hide_the_next_command(
    tmp_path,
    rel,
    gated,
    needs_repo,
):
    """S2: a redirect inside a comment read its target across the backslash-newline,
    joining the next line — a separate command the shell runs — into a filename.
    MEASURED allowed on main for all three."""
    home = tmp_path / "home_cmt"
    home.mkdir()
    cwd = _repo(tmp_path, "repo_cmt") if needs_repo else _REPO_ROOT
    res = _run(rel, "echo a # >x" + _MID + gated, home, cwd)
    assert _decision(res) == "block", res.stdout[:300] + res.stderr[:300]


@pytest.mark.parametrize(
    "command",
    [
        'echo a # a \\" b "\ngit clean -fdx',
        "echo x # it's a note\ngit clean -fdx",
        'cat <<EOF\na \\" b "\nEOF\ngit clean -fdx',
    ],
    ids=["comment-escaped-quote", "comment-apostrophe", "heredoc-escaped-quote"],
)
def test_a_quote_in_a_comment_or_heredoc_does_not_hide_the_next_line(tmp_path, command):
    """S3: in a comment or a heredoc body the shell reads a quote and a backslash
    literally. Read as openers, they swallowed the lines after them and the discard
    guard (which deliberately allows an untokenizable command) let a `git clean`
    through. The apostrophe case was open on main too."""
    home = tmp_path / "home_s3"
    home.mkdir()
    res = _run("hooks/git_discard_guard.py", command, home, _REPO_ROOT)
    assert res.returncode == 2, res.stderr[:300]


# A commit-message heredoc whose prose has an issue reference in parentheses: the
# `(#` opens a comment under the new comment rule, the apostrophe on that line goes
# literal, and the next apostrophe opens a span that runs past the heredoc's end.
_SHIFTED_PROSE = (
    "cat > msg.txt <<'EOF'\n"
    "fix: rename the migration (#12) the rebase didn't flag it\n"
    "(two files, no conflict), but the runner's guard fails\n"
    "EOF\n"
)


def test_a_quote_shifted_by_a_heredoc_comment_does_not_hide_the_command_after_it():
    """MEASURED on a real command corpus: this shape hid a `git push` and three
    `git commit`s from every guard, with no blind spot reported, because `shlex`
    has no comment rule and tokenized the command fine. The older scanner's
    quoting pairs the two apostrophes and sees the push; both readings are kept."""
    command = _SHIFTED_PROSE + "git push origin main"
    heads = [p.argv_src.split()[:2] for p in sp.parse_segments(command)]
    assert ["git", "push"] in heads, heads


@pytest.mark.parametrize(
    ("rel", "command"),
    [
        ("hooks/git_push_guard.py", "git\\\n push origin main " + _FORCE),
        ("hooks/git_discard_guard.py", "git\\\n clean -fdx"),
    ],
    ids=["push", "discard"],
)
def test_a_program_split_from_its_verb_with_no_space_is_refused(tmp_path, rel, command):
    """The segments are stripped, so the group text cannot see whether the shell had
    whitespace at the split. Joined directly this reads `gitpush`, which names no
    program; joined with a space it reads what the shell runs. Both are searched."""
    home = tmp_path / "home_nospace"
    home.mkdir()
    res = _run(rel, command, home, _REPO_ROOT)
    assert _decision(res) == "block", res.stdout[:300] + res.stderr[:300]


def test_a_verb_split_after_an_unparseable_span_is_still_refused(tmp_path):
    """When the scanner's model of an earlier word is narrower than the shell's, it can
    read the rest of the line as one word, leaving no continuation group for a split
    verb to be found in. The fallback is a mention test over the text, and the raw text
    of `pu<continuation>sh` names no push, so it reads the continuation-folded text
    too. The shell runs a push here."""
    home = tmp_path / "home_ansic"
    home.mkdir()
    res = _run(
        "hooks/git_push_guard.py",
        "echo $'a\\'b' ; git pu\\\nsh origin main",
        home,
        _REPO_ROOT,
    )
    assert _decision(res) == "block", res.stdout[:300] + res.stderr[:300]


@pytest.mark.parametrize(
    ("rel", "gated"),
    [
        ("hooks/git_push_guard.py", "git push origin main " + _FORCE),
        ("hooks/git_discard_guard.py", "git clean -fdx"),
    ],
    ids=["push", "discard"],
)
def test_a_heredoc_quote_shift_does_not_let_a_gated_command_through(tmp_path, rel, gated):
    home = tmp_path / "home_shift"
    home.mkdir()
    one_line = _run(rel, gated, home, _REPO_ROOT)
    assert _decision(one_line) == "block", one_line.stderr[:300]
    res = _run(rel, _SHIFTED_PROSE + gated, home, _REPO_ROOT)
    assert _decision(res) == "block", res.stdout[:300] + res.stderr[:300]


@pytest.mark.parametrize(
    ("rel", "command"),
    [
        ("hooks/git_discard_guard.py", 'echo "it\'s $(git clean -fdx)"'),
        ("hooks/destructive_command_guard.py", 'echo "it\'s $(rm -rf ~)"'),
        ("hooks/git_discard_guard.py", 'echo "x `git \\"clean\\" -fdx`"'),
    ],
    ids=["discard-dollar", "destructive-dollar", "discard-backtick-escaped-quote"],
)
def test_a_substitution_after_an_apostrophe_in_double_quotes_is_seen(tmp_path, rel, command):
    """S5: an apostrophe inside double quotes is a literal, but the substitution
    extractor read it as a single-quote opener and hid every substitution after it
    from all consumers. Open on main."""
    home = tmp_path / "home_s5"
    home.mkdir()
    res = _run(rel, command, home, _REPO_ROOT)
    assert res.returncode == 2, res.stderr[:300]


@pytest.mark.parametrize(
    ("rel", "command"),
    [
        ("hooks/destructive_command_guard.py", "docker run --rm" + _CONT + "alpine true"),
        ("hooks/destructive_command_guard.py", "rm -f /tmp/scratch.txt" + _CONT + "/tmp/other.txt"),
        ("hooks/git_push_guard.py", 'echo "merge later"' + _CONT + "&& ls"),
        # `git status` first: without a `git` in the command the guard exits at its
        # first early-out and the cell never reaches the group check it is about.
        ("hooks/git_discard_guard.py", 'git status && echo "working tree clean"' + _CONT + "&& ls"),
    ],
    ids=["docker-rm", "shallow-rm", "push-prose", "discard-prose"],
)
def test_a_continued_command_that_is_not_gated_is_allowed(tmp_path, rel, command):
    """S4: the continuation refusal is scoped to the continued command and to what
    it names. MEASURED on the unscoped version: ordinary continued commands that
    merely contained `rm`, "merge" or "clean" were refused."""
    home = tmp_path / "home_s4"
    home.mkdir()
    res = _run(rel, command, home, _REPO_ROOT)
    assert res.returncode == 0, res.stderr[:300]


def test_a_continued_carrier_removal_is_still_refused(tmp_path):
    """The continuation cause hands the destructive guard back to its own scan with
    its carrier refusal ON, so a launcher split from its payload is still caught."""
    home = tmp_path / "home_carrier"
    home.mkdir()
    res = _run(
        "hooks/destructive_command_guard.py", "eval" + _CONT + "'rm -rf ~'", home, _REPO_ROOT
    )
    assert res.returncode == 2, res.stderr[:300]


def test_a_heredoc_that_mentions_commit_is_not_refused_as_a_commit(tmp_path):
    """S4: a continued command that only MENTIONS a commit (a database `.commit()`
    in a heredoc) is not a git commit, and the commit gate no longer refuses it."""
    home = tmp_path / "home_hd"
    home.mkdir()
    repo = _repo(tmp_path, "repo_hd")
    cmd = "python3 - <<'PY'" + "\nconn.commit()\nPY\necho done" + _CONT + "&& ls"
    res = _run("review_enforcement_commit.py", cmd, home, repo)
    assert res.returncode == 0, res.stdout[:300] + res.stderr[:300]


@pytest.mark.parametrize(
    "command",
    ["echo a \\\r\nls", "echo a \\  \nls", "echo a # note \\\nls"],
    ids=["backslash-cr", "backslash-spaces", "comment-backslash"],
)
def test_backslashes_the_shell_does_not_continue_are_not_reported(command):
    """Three former over-reports: the scanner records continuations itself now, and
    none of these is one. A backslash before CR escapes the CR; before spaces it
    escapes a space; at the end of a comment it is comment text."""
    assert _cause(command) is None


def test_the_mention_view_widens_and_never_narrows():
    raw = "git cl" + _MID + "ean -fdx"
    view = sp.mention_view(raw)
    assert raw in view and "git clean -fdx" in view
    assert sp.mention_view("git status") == "git status"
