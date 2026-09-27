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

from shell_parse import _is_prefix_assignment, _net_subshell_depth, analyze  # noqa: E402


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
