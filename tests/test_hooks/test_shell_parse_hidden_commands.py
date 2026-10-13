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
    _is_prefix_assignment,
    _net_subshell_depth,
    analyze,
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
