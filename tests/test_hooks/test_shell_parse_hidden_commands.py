"""Spellings that made a command invisible to every guard.

``shell_parse`` resolves the command a segment actually runs; every guard keys on
that result. Three spellings resolved to the wrong command, so the guard saw no
push, merge or delete at all — MEASURED through the live push guard, each of
these returned NO decision, including a push onto the default branch:

    FOO+=1 git push origin HEAD:main
    env A.B=1 git push origin HEAD:main
    env =x git push origin HEAD:main
    (export A=1; git push)

and in real bash each one runs the git command.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))

from shell_parse import (  # noqa: E402
    Segment,
    _is_prefix_assignment,
    _keep_substitutions_whole,
    _net_subshell_depth,
    analyze,
    analyze_checked,
)


def _git_argv(command: str) -> list[str]:
    segs = [s for s in analyze(command) if s.exe == "git"]
    assert segs, f"no git segment resolved from {command!r} — the command is hidden"
    return segs[-1].argv


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # bash prefix assignment with `+=` — bash runs the command with FOO set.
        ("FOO+=1 git push origin HEAD:main", ["git", "push", "origin", "HEAD:main"]),
        # `env` takes ANY word containing `=` as an assignment (measured).
        ("env A.B=1 git push", ["git", "push"]),
        ("env 1X=1 git push", ["git", "push"]),
        ("env =x git push", ["git", "push"]),
        # a subshell opened by an EARLIER segment closes on this one.
        ("(export A=1; git push)", ["git", "push"]),
        ("(true; (false; git push))", ["git", "push"]),
        ("(echo $(date); git push)", ["git", "push"]),
    ],
)
def test_the_hidden_command_is_resolved(command: str, expected: list[str]) -> None:
    assert _git_argv(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        '(echo "a)"; git push)',
        "(echo 'a)'; git push)",
        "(echo a\\); git push)",
    ],
)
def test_a_QUOTED_closer_does_not_cancel_the_subshell(command: str) -> None:
    """The first, token-based depth counter was defeated by these: shlex drops the
    quotes, so the quoted `a)` looked like a closer, cancelled the opener, and the
    push hid again. The count runs on source text, outside quotes."""
    assert _git_argv(command) == ["git", "push"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push", ["git", "push"]),
        ("(git push)", ["git", "push"]),
        ('git commit -m "(wip) x)"', ["git", "commit", "-m", "(wip) x)"]),
        ("FOO=1 git push", ["git", "push"]),
    ],
)
def test_ordinary_forms_are_unchanged(command: str, expected: list[str]) -> None:
    """CONTROL — a quoted paren inside an argument must survive untouched."""
    assert _git_argv(command) == expected


@pytest.mark.parametrize(
    ("tok", "is_assignment"),
    [
        ("FOO=1", True),
        ("FOO+=1", True),
        ("_x=", True),
        ("a[0]=1", True),
        ("1X=1", False),
        ("+=1", False),
        ("git", False),
    ],
)
def test_which_words_bash_takes_as_a_prefix_assignment(tok: str, is_assignment: bool) -> None:
    """`a[0]=1 cmd` prints "not a valid identifier" and then RUNS cmd (MEASURED,
    bash 5.2). An earlier revision asserted False here from a probe piped through
    `head -1`, which discarded the line proving the command ran."""
    assert _is_prefix_assignment(tok) is is_assignment


@pytest.mark.parametrize(
    ("src", "depth"),
    [
        ("(a", 1),
        ("a)", -1),
        ("(a)", 0),
        ('"(a"', 0),
        ("'a)'", 0),
        ("a\\)", 0),
        ("$(a)", 0),
        ("((a", 2),
    ],
)
def test_subshell_depth_counts_only_unquoted_parens(src: str, depth: int) -> None:
    assert _net_subshell_depth(src) == depth


@pytest.mark.parametrize(
    "command",
    [
        "nohup ./a=b/git push origin HEAD:main",
        "timeout 5 /opt/k=v/git push origin HEAD:main",
        "nice ./a=b/git push",
        "nohup A=b/git push",
    ],
)
def test_a_NON_assignment_wrapper_executes_an_equals_word(command: str) -> None:
    """The negative direction, where a real regression lived: these wrappers
    EXECUTE a word containing `=` (MEASURED in bash: `nohup ./a=b/git push` runs
    git). Skipping it as an assignment, as an earlier revision did for every
    wrapper, resolved the command to `push` and hid it."""
    segs = analyze(command)
    assert segs[0].argv[0].endswith("git"), segs[0].argv


def test_a_subscripted_prefix_assignment_does_not_hide_the_command() -> None:
    assert _git_argv("a[0]=1 git push origin HEAD:main") == ["git", "push", "origin", "HEAD:main"]


def test_a_paren_inside_a_comment_is_text() -> None:
    assert _git_argv("(echo hi # :)\ngit push)") == ["git", "push"]


# ---------------------------------------------------------------------------
# Wrapper operand grammar, MEASURED in bash 5.2 / GNU env 9.4 / sudo 1.9.15 with
# a stub for every candidate program, reading which one actually ran.


@pytest.mark.parametrize(
    "command",
    [
        "env -- A=1 git push",
        "env -- A.B=1 git push",
        "env -- =x git push",
        "env -u X -- A=1 git push",
        "timeout -- 5 git push",
    ],
)
def test_the_option_terminator_does_not_end_wrapper_operands(command: str) -> None:
    """`--` ends a wrapper's OPTIONS only: `env -- A=1 cmd` still assigns A and
    `timeout -- 5 cmd` still takes 5 as the duration, and both run git."""
    assert _git_argv(command) == ["git", "push"]


@pytest.mark.parametrize(
    "command",
    [
        "time A=1 git push",
        "time -p A=1 git push",
        "time -- A=1 git push",
        "time time A=1 git push",
        "! time A=1 git push",
        "time ! A=1 git push",
    ],
)
def test_the_time_keyword_keeps_prefix_assignments(command: str) -> None:
    """A bare `time` is the bash keyword: the simple command after it may start
    with assignments, and bash runs git."""
    assert _git_argv(command) == ["git", "push"]


@pytest.mark.parametrize(
    ("command", "exe"),
    [
        # a `time` reached through a wrapper is the external program, which
        # executes a `=`-word
        ("env time A=1 git push", "A=1"),
        # sudo runs an absolute path containing `=`, and a word starting with `=`
        ("sudo /opt/k=v/git push", "git"),
        ("sudo =x git push", "=x"),
        # after `--` sudo takes no assignments
        ("sudo -- A=1 git push", "A=1"),
        # env takes EVERY `=`-word, a path included, and runs what follows
        ("env /opt/k=v/git push", "push"),
    ],
)
def test_wrapper_equals_words_resolve_to_what_runs(command: str, exe: str) -> None:
    assert analyze(command)[0].exe == exe


def test_sudo_keeps_the_absolute_path_it_executes() -> None:
    assert analyze("sudo /opt/k=v/git push")[0].argv == ["/opt/k=v/git", "push"]


def test_sudo_still_takes_an_ordinary_assignment() -> None:
    assert _git_argv("sudo A.B=1 git push") == ["git", "push"]


@pytest.mark.parametrize(
    "command",
    [
        "(echo x; git push origin 'HEAD:main)'; echo done)",
        '(echo x; git push origin "HEAD:main)"; echo done)',
        "(git push origin 'HEAD:main)'; echo done)",
    ],
)
def test_a_quoted_trailing_paren_inside_a_subshell_over_gates(command: str) -> None:
    """ACCEPTED over-gate: the peel is by token, so the quoted `)` of `main)` is
    peeled and the ref reads `HEAD:main` (bash pushes to `main)`). Reading quoting
    from the source to avoid it was tried and removed — its failure mode hid the
    push. The push itself must stay visible."""
    assert _git_argv(command) == ["git", "push", "origin", "HEAD:main"]


@pytest.mark.parametrize(
    "command",
    [
        "(echo x; git push origin 'HEAD:main)')",
        "(git push origin 'HEAD:main)')",
    ],
)
def test_a_quoted_paren_right_before_the_real_closer_is_kept(command: str) -> None:
    """Here the real closer is the last character, so the one peel removes it and
    the ref keeps its own `)` — the case the over-gate above does not reach."""
    assert _git_argv(command) == ["git", "push", "origin", "HEAD:main)"]


@pytest.mark.parametrize(
    "command",
    [
        "(A=${v:-{} git push)",
        "(A=${v//[{]/} git push)",
        "(A=$'\\'' git push)",
        "(echo x; A=$'\\'' git push)",
        '(A="$(echo "(")" git push)',
        "(A=${v:-${w:-{}} git push)",
        "(echo ${v:-)}; git push)",
    ],
)
def test_quoting_the_source_scan_misreads_never_hides_the_verb(command: str) -> None:
    """Each of these runs the push in bash (MEASURED). A source-text quote reading
    that decided the peel left `push)` glued here, so the peel stays by token."""
    assert any(s.exe == "git" and s.argv[-1] == "push" for s in analyze(command)), analyze(command)


@pytest.mark.parametrize(
    "command",
    [
        "(git -c a.b=${v//[(]/} push)",
        "(git -c a.b=${v/(/} push)",
        "(git -c a.b=${v:-(} push)",
    ],
)
def test_a_paren_inside_a_parameter_expansion_is_not_a_subshell(command: str) -> None:
    """A `(` inside `${...}` is pattern text. Counting it as an opener made the
    source look like it closed nothing, the real closer stayed glued to the verb,
    and the guard read `push)` — no push."""
    assert _git_argv(command)[-1] == "push"


@pytest.mark.parametrize(
    "command",
    [
        "sudo ~/k=v/git push",
        "sudo $HOME/k=v/git push",
        "sudo ${PWD}/k=v/git push",
        'sudo "$PWD"/k=v/git push',
    ],
)
def test_sudo_runs_a_path_that_expands_to_an_absolute_one(command: str) -> None:
    """sudo tests the EXPANDED word; these reach it as absolute paths and run."""
    seg = analyze(command)[0]
    assert (seg.exe, seg.argv[1:]) == ("git", ["push"])


def test_sudo_takes_an_expanded_assignment_without_a_slash() -> None:
    """`sudo $X=1 cmd` with X=A reaches sudo as `A=1` and runs cmd."""
    assert _git_argv("sudo $X=1 git push") == ["git", "push"]


# ---------------------------------------------------------------------------
# A word split by a command substitution. shlex splits inside `$(…)`, bash does
# not, so a walk that skips a word by position landed on the substitution's tail.
# The fix reads each segment a second way, with substitutions kept whole, and
# ADDS what that reading resolves; it never replaces the ordinary reading.

_PUSH = "pu" + "sh"


def _pushes(command: str) -> list[Segment]:
    return [s for s in analyze(command) if s.exe == "git" and s.argv[1:2] == [_PUSH]]


@pytest.mark.parametrize(
    "command",
    [
        "X=$(id -u) git push origin main",
        "X+=$(id -u) git push origin main",
        "X=`id -u` git push origin main",
        "A=1 X=$(echo a b c) git push origin main",
        "X=$(echo a $(echo b c)) git push origin main",
        "env X=$(id x) git push origin main",
        "sudo X=$(id x) git push origin main",
        "timeout -s $(echo KILL x) 5 git push origin main",
        "nice -n $(echo 1 2) git push origin main",
        "timeout $(echo 5 6) git push origin main",
        # A quoted `)` inside the body is data; the word ends at the real close.
        "X=$(echo ')' a) git push origin main",
        'X=$(echo")" a b) git push origin main',
        "X=$(echo\\) a b) git push origin main",
        'env X=$(echo")" a b) git push origin main',
        '(X=$(echo")" a b) git push origin main)',
        # An arithmetic expansion is one word as well.
        "X=$((1 + 2)) git push origin main",
        "env X=$((1 + 2)) git push origin main",
        # Both readings name git, with different operations.
        "X=$(command git rev-parse HEAD) git push origin main",
        "X=$(echo git status) git push origin main",
        # Double quotes nested inside a double-quoted substitution.
        'X="$(echo "a b")" git push origin main',
    ],
)
def test_a_word_split_by_a_substitution_does_not_hide_the_command(command: str) -> None:
    """`X=$(id -u) git push` resolved to a program named `-u)`: every guard saw
    no push, and no blind spot was reported. MEASURED on main for each form."""
    assert len(_pushes(command)) == 1


@pytest.mark.parametrize(
    "command",
    [
        # A single-quoted `$(` is literal text.
        "X='$(' git push origin 'x)'",
        # Constructs the scanner does not model end its span after bash does;
        # an earlier revision that REPLACED the ordinary reading hid each push.
        "X=$(:${x:-(})y git push origin main $(:)",
        "X=$(:${x%(*})y git push origin $(echo main)",
        "X=$(:${x:-(})y git push origin main ${y%)}",
        "X=$(:`:\\`(\\``)y git push origin main '`))'\\'",
        # Case patterns that only look like substitutions once shlex dequotes.
        "case '$(' in '$(') git push origin main;; esac",
        "case '`' in '`') git push origin main;; esac",
    ],
)
def test_the_second_reading_never_hides_what_the_first_resolves(command: str) -> None:
    """Every push the ordinary reading resolves is still there. A mistake in
    the second reading can only add a segment."""
    assert _pushes(command)


def test_one_push_is_counted_once() -> None:
    """Where both readings name the same program, the second adds nothing: a
    duplicate would read as two publish operations in one command."""
    assert len(_pushes("git push origin $(git branch --show-current)")) == 1
    assert len(_pushes("X=$(id -u) git push origin main")) == 1


def test_the_ordinary_reading_and_its_depth_zero_segments_are_unchanged() -> None:
    """The second reading is reported nested, so the depth-0 segments still map
    one to one onto the command's segments, as the cwd consumers require."""
    segments = analyze("X=$(id -u) git status; echo done")
    assert [s.depth for s in segments if s.exe in ("-u)", "echo")] == [0, 0]
    assert any(s.exe == "git" and s.depth == 1 for s in segments)


def test_many_prefix_substitutions_stay_within_the_nesting_bound() -> None:
    command = " ".join(f"V=$(echo {i})" for i in range(6)) + " python -m pytest tests/x.py"
    segments, blind = analyze_checked(command)
    assert blind is None
    assert any(s.exe == "python" for s in segments)


def test_an_unterminated_substitution_is_left_as_it_was() -> None:
    """Bash refuses it as a syntax error and runs none of it."""
    command = "X=$(id -u git push origin main"
    assert _keep_substitutions_whole(command) == command


def test_a_prefix_substitution_is_not_read_as_a_case_pattern() -> None:
    """`X=$(id)` ends in its substitution's `)`. Read as a case pattern, the rest
    was parsed again one level down and one push counted as two."""
    assert len(_pushes("X=$(id) git push origin feature-x")) == 1


_EVERY_FORM = [
    "X=$(id -u) git push origin main",
    'X=$(echo")" a b) git push origin main',
    "X=$(command git rev-parse HEAD) git push origin main",
    "X=$(:${x:-(})y git push origin main $(:)",
    "X=$(:`:\\`(\\``)y git push origin main '`))'\\'",
    "case '$(' in '$(') git push origin main;; esac",
    "git push origin $(git branch --show-current)",
    "timeout -s $(echo KILL x) 5 git push origin main",
    "echo \"$(date) x\" ; rm -rf /tmp/zz",
]


@pytest.mark.parametrize("command", _EVERY_FORM)
def test_the_second_reading_only_ever_adds(command: str, monkeypatch) -> None:
    """The property, not an example: with the second reading switched off, every
    segment the parse produces is still produced with it switched on."""
    import shell_parse

    with_it = [(s.exe, tuple(s.argv), s.depth) for s in analyze(command)]
    monkeypatch.setattr(shell_parse, "_whole_word_reading", lambda *a, **k: None)
    without = [(s.exe, tuple(s.argv), s.depth) for s in analyze(command)]
    remaining = list(with_it)
    for seg in without:
        assert seg in remaining, f"{seg} disappeared"
        remaining.remove(seg)
