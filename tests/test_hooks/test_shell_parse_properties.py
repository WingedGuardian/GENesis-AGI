"""Property tests for ``scripts/hooks/shell_parse.py`` (#3187).

Each property states an invariant the guards rely on and is checked over
generated inputs.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
import shell_parse as sp  # noqa: E402

PROFILE = settings(max_examples=200, derandomize=True, database=None, deadline=None)

_OPERATORS = [" && ", " || ", "; ", " | ", "\n"]
_TOKENS = [
    "git",
    "push",
    "echo",
    "a",
    "'",
    '"',
    "\\",
    "$(",
    ")",
    "`",
    "&&",
    "||",
    ";",
    "|",
    "\n",
    " ",
    "<<",
    "<<'EOF'",
    "EOF",
    ">",
    "2>&1",
    "{",
    "}",
    "(",
    "#",
    "$X",
    "-o",
    "=",
]
# Lone surrogates (Cs) are excluded by st.text() unless asked for explicitly.
any_text = st.text(st.characters(categories=["L", "N", "P", "S", "Z", "Cc", "Cs"]), max_size=200)
shellish = st.lists(st.sampled_from(_TOKENS), max_size=60).map("".join)
command_text = st.one_of(any_text, shellish)
nested = st.integers(1, sp.MAX_SUBSTITUTION_DEPTH + 3).map(
    lambda n: "echo " + "$(echo " * n + "x" + ")" * n
)
oversized = st.integers(-2, 2).map(lambda d: "echo " + "a" * (sp.MAX_COMMAND_CHARS - 5 + d))
word = st.text(
    st.characters(
        categories=["L", "N", "P", "S", "Zs", "Cc"],
        include_characters="&|;()<>`$'\"\\\n\t#!{}*?[]~=",
        exclude_characters="\x00",
    ),
    max_size=15,
)


@PROFILE
@given(command_text)
def test_parser_entry_points_never_raise(command: str) -> None:
    sp.parse_segments(command)
    sp.split_segments(command)
    sp.analyze_checked(command)
    sp.untokenizable(command)


@PROFILE
@given(st.lists(word, max_size=6))
def test_a_quoted_argv_parses_back_to_itself(words: list[str]) -> None:
    argv = ["echo", *words]
    segments, blind = sp.analyze_checked(shlex.join(argv))
    assert blind is None
    assert [s.argv for s in segments] == [argv]


@PROFILE
@given(
    st.lists(st.lists(word, max_size=4), min_size=1, max_size=4),
    st.lists(st.sampled_from(_OPERATORS), min_size=3, max_size=3),
)
def test_joined_commands_split_into_exactly_those_commands(
    argvs: list[list[str]], operators: list[str]
) -> None:
    argvs = [["echo", *a] for a in argvs]
    parts = [shlex.join(a) for a in argvs]
    command = parts[0] + "".join(op + part for op, part in zip(operators, parts[1:], strict=False))
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    assert [s.argv for s in segments] == argvs


@PROFILE
@given(st.one_of(command_text, nested, oversized))
def test_a_parse_cut_short_by_a_bound_returns_no_segments(command: str) -> None:
    segments, blind = sp.analyze_checked(command)
    if blind is not None and blind.bounds_induced:
        assert segments == []


@PROFILE
@given(
    command_text,
    st.integers(sp.MAX_SUBSTITUTION_DEPTH + 1, sp.MAX_SUBSTITUTION_DEPTH + 4),
    st.integers(1, 64),
    st.booleans(),
)
def test_an_input_past_a_bound_returns_no_segments(
    noise: str, depth: int, extra: int, deep: bool
) -> None:
    bounded = (
        "echo " + "$(echo " * depth + "x" + ")" * depth
        if deep
        else "a" * (sp.MAX_COMMAND_CHARS + extra)
    )
    segments, blind = sp.analyze_checked(f"echo ok; {bounded}; {noise}")
    assert blind is not None and blind.bounds_induced
    assert segments == []


_PUSH_WORD = st.text(
    st.characters(categories=["L", "N"], include_characters="-+=:/._"), min_size=1, max_size=12
)
_LONG = st.sampled_from(sorted(sp.GIT_PUSH_LONG_OPTIONS))
_PUSH_ARG = st.one_of(
    _PUSH_WORD,
    _LONG.map(lambda o: "--" + o),
    _LONG.map(lambda o: "--no-" + o),
    st.sampled_from(["--", "-f", "-o", "-fo", "-u", "+main", "-q", "-ov"]),
)
_GLOBALS = st.lists(
    st.sampled_from(["-C", "x", "-c", "k=v", "--no-pager", "-p", "status"]), max_size=4
)


@PROFILE
@given(_GLOBALS, st.lists(_PUSH_ARG, max_size=10))
def test_push_arg_stream_is_none_exactly_for_a_non_push(
    globals_: list[str], rest: list[str]
) -> None:
    argv = ["git", *globals_, "push", *rest]
    stream = sp.push_arg_stream(argv)
    index = sp.git_subcommand_index(argv)
    is_push = index is not None and argv[index] == "push"
    assert (stream is not None) == (
        is_push
        and all(
            t.startswith("-") or argv[i - 1] in sp._GIT_OPTS_WITH_ARG
            for i, t in enumerate(argv[1:index], 1)
        )
    )


@PROFILE
@given(
    st.lists(
        st.sampled_from(["-f", "-q", "-u", "--force", "--tags", "--dry-run", "--signed"]),
        max_size=4,
    ),
    st.lists(_PUSH_ARG, max_size=6),
)
def test_everything_after_double_dash_is_positional(options: list[str], tail: list[str]) -> None:
    stream = sp.push_arg_stream(["git", "push", *options, "--", *tail])
    after = stream[len(stream) - len(tail) :] if tail else []
    assert [a.text for a in after] == tail
    assert all(a.kind in (sp.PushArgKind.POSITIONAL, sp.PushArgKind.PLUS) for a in after)


@PROFILE
@given(st.lists(_PUSH_ARG, max_size=10))
def test_an_option_value_always_follows_its_option(rest: list[str]) -> None:
    stream = sp.push_arg_stream(["git", "push", *rest])
    for previous, arg in zip(stream, stream[1:], strict=False):
        if arg.kind is sp.PushArgKind.LONG_VALUE:
            assert previous.kind is sp.PushArgKind.LONG
        if arg.kind is sp.PushArgKind.SHORT_VALUE:
            assert previous.kind is sp.PushArgKind.SHORT
            assert previous.text in sp.GIT_PUSH_SHORT_VALUE_LETTERS


_GIT_PUSH_OPTIONS = [
    "-q",
    "-v",
    "-f",
    "-u",
    "-n",
    "-d",
    "--force",
    "--dry-run",
    "--signed",
    "--force-with-lease",
    "--thin",
    "--atomic",
    "-o",
    "-ov",
    "-vo",
    "-fo",
    "--push-option",
    "--push-o",
    "--push-option=z",
    "--repo",
    "--rep",
    "--repo=Q",
    "--recurse-submodules=no",
    "--",
    "--tags",
    "-4",
]
_REMOTE_NAMED = re.compile(
    r"'([^']*)' does not appear to be a git repository|failed to push some refs to '([^']*)'"
)


@pytest.fixture(scope="module")
def empty_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    root = tmp_path_factory.mktemp("push_remote")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1", HOME=str(root), GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="false"
    )
    subprocess.run(["git", "init", "-q", str(root)], env=env, check=True)
    return root, env


def _stream_remote(stream: list[sp.PushArg]) -> str | None:
    positional = [
        a.text for a in stream if a.kind in (sp.PushArgKind.POSITIONAL, sp.PushArgKind.PLUS)
    ]
    if positional:
        return positional[0]
    repo = None
    for arg, following in zip(stream, [*stream[1:], None], strict=False):
        if arg.kind is sp.PushArgKind.LONG and arg.text.startswith("--repo"):
            if "=" in arg.text:
                repo = arg.text.split("=", 1)[1]
            elif following is not None and following.kind is sp.PushArgKind.LONG_VALUE:
                repo = following.text
    return repo


@PROFILE
@given(
    rest=st.lists(
        st.one_of(st.sampled_from(_GIT_PUSH_OPTIONS), st.integers(0, 9).map(lambda i: f"P{i}")),
        max_size=8,
    )
)
@example(rest=["-o", "P1"])
@example(rest=["-fo", "P1", "P2"])
@example(rest=["--repo", "P1"])
@example(rest=["--rep", "P1", "P2"])
def test_the_remote_matches_the_one_real_git_pushes_to(
    empty_repo: tuple[Path, dict[str, str]], rest: list[str]
) -> None:
    root, env = empty_repo
    result = subprocess.run(
        ["git", "push", *rest], cwd=root, env=env, capture_output=True, text=True, timeout=15
    )
    named = _REMOTE_NAMED.search(result.stderr)
    if named is None:
        assume("No configured push destination" in result.stderr)
    git_remote = (named.group(1) or named.group(2)) if named else None
    assert _stream_remote(sp.push_arg_stream(["git", "push", *rest])) == git_remote


def _git_resolution(empty_repo: tuple[Path, dict[str, str]], prefix: str) -> str | None:
    """The option git resolves ``--<prefix>`` to, or None when git rejects it."""
    root, env = empty_repo
    result = subprocess.run(
        ["git", "push", f"--{prefix}=x"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if re.search(r"unknown option|ambiguous option", result.stderr):
        return None
    named = re.search(r"option `([^']+)' takes no value", result.stderr)
    return named.group(1) if named else "accepted"


_PREFIXES = sorted(
    {
        c[:n]
        for c in sp._GIT_PUSH_LONG_OPTION_CANDIDATES
        if not c.startswith("no-")
        for n in range(1, len(c) + 1)
    }
)


@PROFILE
@given(prefix=st.sampled_from(_PREFIXES))
def test_long_option_abbreviations_resolve_as_real_git_does(
    empty_repo: tuple[Path, dict[str, str]], prefix: str
) -> None:
    ours = sp._canonical_push_long_option(prefix)
    known = ours in sp._GIT_PUSH_LONG_OPTION_CANDIDATES
    git = _git_resolution(empty_repo, prefix)
    assume(not (ours == "branches" and git is None))  # --branches is git >= 2.43
    assert known == (git is not None)
    if git not in (None, "accepted"):
        assert git == ours or (
            ours == "verify" and git == "no-no-verify"
        )  # git < 2.43 spells --verify this way


_PUSH = ["git", "push", "--force", "origin", "main"]
_ESCAPABLE = st.text(
    st.characters(
        categories=["L", "N", "P", "S", "Zs"],
        include_characters="&|;()<>`$'\"\\#!{}*?[]~=",
        exclude_characters="\x00",
    ),
    min_size=1,
    max_size=10,
)


def _pushes(command: str) -> bool:
    segments, blind = sp.analyze_checked(command)
    return blind is not None or _PUSH in [s.argv for s in segments]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#3257: a backslash-escaped quote outside quotes hides the next command",
)
@PROFILE
@given(st.lists(_ESCAPABLE, min_size=1, max_size=4), st.sampled_from(["\n", "; ", " && "]))
@example(words=["It's", "done"], separator="; ")
def test_a_backslash_escaped_word_never_hides_the_next_command(
    words: list[str], separator: str
) -> None:
    escaped = " ".join("".join(c if c.isalnum() else "\\" + c for c in w) for w in words)
    assert _pushes(f"echo {escaped}{separator}{shlex.join(_PUSH)}")


_HEREDOC_HEADS = [
    ("cat <<'EOF'", "EOF"),
    ("cat <<-'EOF'", "EOF"),
    ("cat > notes.md <<'EOF'", "EOF"),
    ("tee out.txt <<'EOF'", "EOF"),
    ("git commit -F - <<'EOF'", "EOF"),
    ("python3 - <<'PY'", "PY"),
]
_BODY_LINE = st.lists(
    st.sampled_from([*(t for t in _TOKENS if t not in ("\\", "\n")), "EOF", "PY", "\t"]),
    max_size=10,
).map("".join)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#3433: a stray backtick in a non-excised quoted heredoc body hides later commands",
)
@PROFILE
@given(st.sampled_from(_HEREDOC_HEADS), st.lists(_BODY_LINE, max_size=6))
@example(head=("tee out.txt <<'EOF'", "EOF"), body=["x<`"])
def test_a_command_after_a_quoted_heredoc_stays_visible(
    head: tuple[str, str], body: list[str]
) -> None:
    opener, delimiter = head
    assume(all(line.lstrip("\t") != delimiter for line in body))
    assert _pushes("\n".join([opener, *body, delimiter, shlex.join(_PUSH)]))
