"""Refusal-only substitution and comment boundaries (#3713, PR 1 of 2).

Each fail-open row is a command that real bash runs while the parser, on main, neither
showed the operation nor reported a blind spot. The parser must now do one or the
other, and the real push guard must refuse. Controls are ordinary commands that must
stay readable, so a refusal rule cannot pass by refusing everything.

The oracle runs every command under bash with recording stand-ins for git and gh, in a
throwaway directory, so nothing real is pushed or merged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(HOOKS))
import shell_parse as sp  # noqa: E402

N = "\n"
PUSH = "git " + "push --force origin main"
PUSH_ARGV = ("git", "push", "--force", "origin", "main")
MERGE = "gh pr " + "merge 5"
SIGIL = "review-" + "override"

_SHIM = (
    '#!/bin/sh\n{ printf "%s\\037" "${0##*/}" "$@"; printf "\\036"; } >> "$ORACLE_LOG"\nexit 0\n'
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("bash") is None:
        pytest.skip("the oracle needs bash")
    root = tmp_path_factory.mktemp("substitution-strict")
    (root / "bin").mkdir()
    for name in ("git", "gh"):
        shim = root / "bin" / name
        shim.write_text(_SHIM, encoding="utf-8")
        shim.chmod(0o755)
    (root / "work").mkdir()
    subprocess.run(["git", "init", "-q", str(root / "repo")], check=True)
    return root


def _ran(command: str, sandbox: Path) -> list[tuple[str, ...]]:
    """The argv of every git/gh bash actually executed for ``command``."""
    log = sandbox / "oracle.log"
    log.write_text("", encoding="utf-8")
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=sandbox / "work",
        env={
            "PATH": f"{sandbox / 'bin'}:/usr/bin:/bin",
            "HOME": str(sandbox / "work"),
            "ORACLE_LOG": str(log),
            "LC_ALL": "C",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    data = log.read_text(encoding="utf-8")
    return [tuple(rec.split("\x1f")[:-1]) for rec in data.split("\x1e") if rec]


def _pushed(command: str, sandbox: Path) -> bool:
    """bash ran the force push (extra trailing words are still the same push)."""
    return any(argv[: len(PUSH_ARGV)] == PUSH_ARGV for argv in _ran(command, sandbox))


def _guard_exit(command: str, sandbox: Path) -> int:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "t",
        "cwd": str(sandbox / "repo"),
    }
    done = subprocess.run(
        [sys.executable, str(HOOKS / "git_push_guard.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=sandbox / "repo",
    )
    return done.returncode


def _shows_or_refuses(command: str, wanted: tuple[str, ...]) -> None:
    """Both checked views: the operation is visible, or nothing is and a bound says why."""
    segments, blind = sp.analyze_checked(command)
    policy, policy_blind = sp.analyze_policy(command)
    if blind is not None and blind.bounds_induced:
        assert segments == [] and policy == [] and policy_blind is not None
        return
    assert any(tuple(s.argv[: len(wanted)]) == wanted for s in segments), (
        f"bash runs {wanted} and the parse neither shows it nor refuses: {blind}"
    )
    assert any(tuple(p.argv[: len(wanted)]) == wanted for p in policy)


# --- commands bash runs a push from, which main parsed clean without the push --------

HIDDEN_PUSH = {
    # A quote inside a real comment carried the legacy scan past the line end. The
    # here-doc turns off the top-level comment skip (#3955), so main kept the lines
    # together, then cut them at the comment.
    "dq after a here-doc": "cat <<E" + N + "x" + N + "E" + N + 'echo # "' + N + PUSH + N + '# "',
    "sq after a here-doc": "cat <<E" + N + "x" + N + "E" + N + "echo # '" + N + PUSH + N + "# '",
    "sq after a here-string": "cat <<<x" + N + "echo # '" + N + PUSH + N + "# '",
    "dq substitution after a here-doc": (
        "cat <<E" + N + "x" + N + "E" + N + 'echo # "$(' + N + PUSH + N + '# )"'
    ),
    # An escaped quote outside quotes opened a quote in the comment scanner.
    "escaped sq before a comment": "echo \\' # \"" + N + PUSH + N + '# "',
    "escaped dq before a comment": 'echo \\" # ' + "'" + N + PUSH + N + "# '",
    # Two substitutions whose bodies hold a quote of the other kind pair up (#3264).
    "paired substitutions": 'x="$(printf \'"\')"; ' + PUSH + '; y="$(printf \'"\')"',
    # An escaped quote outside quotes hid a substitution from extraction (#3635).
    "escaped quotes around a substitution": "echo \\' $(" + PUSH + ") \\'; true",
    # An escaped backtick paired with the real opener after it, so the real
    # substitution read as unclosed and was never extracted.
    "escaped backtick before a real one": "echo \\` `" + PUSH + "`",
    # A `<<` the legacy scan sees but bash does not opens a "body" bash runs: it
    # sits in a quote the escaped quote pairs with, or in a comment.
    "here-doc opener after an escaped quote": (
        "echo \\' x ' <<EOF ' \\'" + N + 'echo # "' + N + PUSH + N + '# "' + N + "EOF"
    ),
    "here-doc opener in a comment": (
        "echo # <<EOF" + N + 'echo # "' + N + PUSH + N + '# "' + N + "EOF"
    ),
    # With a `#` anywhere in the command the segmenter leaves escapes unmodelled, so
    # an escaped quote opens a quote that swallows the separator and the push.
    "escaped sq joining segments": "echo \\' ; " + PUSH + ' "\'" #',
    "escaped dq joining segments": 'echo \\" ; ' + PUSH + " '\"' #",
    "escaped sq after a comment line": "true # c" + N + "echo \\' ; " + PUSH + ' "\'"',
    # Only the decoded script holds the misread, so only its own check can see it.
    "inside a double-quoted bash -c": (
        'bash -c "cat <<E' + N + "x" + N + "E" + N + "echo # '" + N + PUSH + N + "# '\""
    ),
}

# Already guarded on main; kept as integration controls, not RED evidence.
HIDDEN_PUSH_CONTROLS = {
    "comment-held substitution": 'echo # "$(' + N + PUSH + N + '# )"',
    "comment-held backtick": 'echo # "`' + N + PUSH + N + '# `"',
    "comment-held substitution in bash -c": "bash -c 'echo # \"$(" + N + PUSH + N + "# )\"'",
    "comment-held backtick in bash -c": "bash -c 'echo # \"`" + N + PUSH + N + "# `\"'",
    "plain": PUSH,
}


@pytest.mark.parametrize("command", HIDDEN_PUSH.values(), ids=HIDDEN_PUSH)
def test_a_push_bash_runs_is_shown_or_refused(command, sandbox):
    assert _pushed(command, sandbox), "fixture: bash must really run the push"
    _shows_or_refuses(command, PUSH_ARGV)
    assert _guard_exit(command, sandbox) == 2


@pytest.mark.parametrize("command", HIDDEN_PUSH_CONTROLS.values(), ids=HIDDEN_PUSH_CONTROLS)
def test_pushes_main_already_saw_stay_refused(command, sandbox):
    assert _pushed(command, sandbox), "fixture: bash must really run the push"
    _shows_or_refuses(command, PUSH_ARGV)
    assert _guard_exit(command, sandbox) == 2


# --- a flag bash passes, which main dropped -----------------------------------------

HIDDEN_FLAG = {
    "hash in a quoted substitution body": 'git commit -m "$(printf "a # b")" --no-verify',
    "hash and newline in a quoted substitution body": (
        'git commit -m "$(printf "hello # world' + N + '")" --no-verify'
    ),
    # bash treats only space, tab and newline as the blank before a comment.
    "vertical tab before a hash": "git commit -m msg\v# --no-verify",
    "form feed before a hash": "git commit -m msg\f# --no-verify",
    "no-break space before a hash": "git commit -m msg # --no-verify",
}


@pytest.mark.parametrize("command", HIDDEN_FLAG.values(), ids=HIDDEN_FLAG)
def test_a_flag_bash_passes_is_shown_or_refused(command, sandbox):
    assert any("--no-verify" in argv for argv in _ran(command, sandbox))
    segments, blind = sp.analyze_checked(command)
    if blind is not None and blind.bounds_induced:
        assert segments == []
    else:
        assert any("--no-verify" in s.argv for s in segments)


# --- an override sigil bash never reads as a comment ---------------------------------

FORGED = {
    "inside a quoted substitution body": MERGE + ' "$(printf "x # ' + SIGIL + '")"',
    "inside a quoted backtick body": MERGE + ' "`printf "x # ' + SIGIL + '"`"',
    "after an escaped dq": MERGE + ' \\"x" # ' + SIGIL + '"',
    "after an escaped sq": MERGE + " \\'x' # " + SIGIL + "'",
    "after a vertical tab": MERGE + " x\v# " + SIGIL,
    # The comment belongs to the command inside the substitution.
    "inside a substitution": MERGE + " --squash $(true # " + SIGIL + N + ")",
    "inside a backtick substitution": MERGE + " --squash `true # " + SIGIL + "`",
}


def test_an_escaped_quote_never_makes_an_override_count():
    """bash reads this comment, and main did not honour it. The override scan may only
    switch an override off, so it stays off rather than starting to count."""
    segments, blind = sp.analyze_checked(MERGE + " it\\'s # " + SIGIL)
    if blind is not None and blind.bounds_induced:
        assert segments == []
    else:
        assert [s.override for s in segments if s.exe == "gh"] == [False]
    assert not sp.has_trailing_override(MERGE + " it\\'s # " + SIGIL)


@pytest.mark.parametrize("command", FORGED.values(), ids=FORGED)
def test_a_forged_override_does_not_authorize(command):
    segments, blind = sp.analyze_checked(command)
    if blind is not None and blind.bounds_induced:
        assert segments == []
        return
    merge = [s for s in segments if s.exe == "gh"]
    assert merge and not any(s.override for s in merge)


def test_a_substitution_local_override_stays_inside_it():
    """The comment belongs to the inner command; the enclosing merge is not waived."""
    segments, blind = sp.analyze_checked(MERGE + " `echo # " + SIGIL + "`")
    assert blind is None
    by_exe = {s.exe: s.override for s in segments}
    assert by_exe == {"gh": False, "echo": True}


# --- refusals the spec asks for, kept as stated costs --------------------------------

ACCEPTED_REFUSALS = {
    # A backslash beside an opener outside quotes, and a backtick body with one.
    "escaped backtick pair": "echo It\\'s; echo \\`" + PUSH + "\\`",
    "backslash in a backtick body": "x=`echo a\\\\b`",
    "escaped substitution opener": "echo \\$(" + PUSH + ")",
    # A decoded -c script holding \$ or \`; bash prints them, nothing runs.
    "escaped dollar in a -c script": "bash -c 'echo \\$HOME'",
}


def test_an_unproven_boundary_outranks_only_an_unresolved_verb():
    """Main reports the hidden verb, which does not withhold; the boundary does."""
    command = "git $V; git commit -m " + '"$(printf "a # b")"'
    segments, blind = sp.analyze_checked(command)
    assert blind is sp._BLIND_STRUCTURAL and segments == []


@pytest.mark.parametrize("command", ACCEPTED_REFUSALS.values(), ids=ACCEPTED_REFUSALS)
def test_ambiguous_backslash_forms_refuse(command):
    segments, blind = sp.analyze_checked(command)
    assert blind is not None and blind.bounds_induced and segments == []


# --- ordinary commands that must stay readable ---------------------------------------

READABLE = {
    # Extraction still reads inside comments, as on main: over-inclusive, never hiding.
    "comment with a substitution in it": ("git status # see $(x)", ["git", "x"]),
    "comment with an unmatched backtick": ("git status # check ` later", ["git"]),
    "hash inside quotes": ('git commit -m "fix #12"', ["git"]),
    "real override": ("gh pr merge 5 --squash # " + SIGIL, ["gh"]),
    "quoted message with a quote in its heredoc body": (
        "git commit -m \"$(cat <<'EOF'" + N + 'fix "foo" bar' + N + "EOF" + N + ')"',
        ["git", "cat"],
    ),
    "heading with an apostrophe in a message heredoc": (
        "git commit -m \"$(cat <<'EOF'" + N + "## What's new" + N + "EOF" + N + ')"',
        ["git", "cat"],
    ),
    "nested quoted substitutions": ('cp "$(basename "$f")" /x', ["cp", "basename"]),
    "nested quoted substitution in a substitution": (
        'echo "$(basename "$(pwd)")"',
        ["echo", "basename", "pwd"],
    ),
    "quoted redirect in a substitution": (
        'x="$(grep -c "a" "$f" 2>/dev/null)"',
        ["", "grep"],
    ),
    "printf format in a substitution": ('x="$(printf "%s" "$y")"', ["", "printf"]),
    "escaped dollar outside quotes": ("echo \\$HOME", ["echo"]),
    # A lone escaped backtick has nothing to pair with: bash and the scans agree.
    "lone escaped backtick": ("ls a\\`b; git status", ["ls", "git"]),
    "escaped quotes around a date": ('echo \\"$(date)\\"', ["echo", "date"]),
    "multi-line substitution": ("x=$(" + N + "ls" + N + ")", ["", "ls", ""]),
    # The body is data: its quoted comment lines are not shell.
    "quoted comment lines in a here-doc body": (
        "python3 - <<'PY'" + N + '# note "' + N + "x = 1" + N + '# "' + N + "PY",
        ["python3", "", "PY"],
    ),
}


@pytest.mark.parametrize(("command", "exes"), READABLE.values(), ids=READABLE)
def test_ordinary_commands_stay_readable(command, exes):
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    assert [s.exe for s in segments] == exes


def test_a_real_override_is_still_honoured():
    segments, _ = sp.analyze_checked("gh pr merge 5 --squash # " + SIGIL)
    assert [s.override for s in segments] == [True]


@pytest.mark.parametrize(
    "command",
    [
        "git status # see it's fine",
        # A body the here-doc check verified is data: its lines are not shell.
        "python3 - <<'PY'" + N + "# it's data" + N + "print(1)" + N + "PY",
    ],
)
def test_the_new_rule_never_outranks_an_existing_cause(command):
    """An apostrophe the tokenizer cannot pair is untokenizable, and that cause keeps
    its rank: the discard guard's exemption for it is a measured decision."""
    assert sp.analyze_checked(command)[1] is sp._BLIND_UNTOKENIZABLE


def test_a_proven_body_does_not_clear_a_sibling_boundary(sandbox):
    """The heredoc body inside the substitution is data; the line after it is not."""
    command = (
        "x=\"$(cat <<'EOF'"
        + N
        + "# it's data"
        + N
        + "EOF"
        + N
        + ')"'
        + N
        + 'echo # "'
        + N
        + PUSH
        + N
        + '# "'
    )
    assert _pushed(command, sandbox), "fixture: bash must really run the push"
    _shows_or_refuses(command, PUSH_ARGV)
    assert _guard_exit(command, sandbox) == 2
