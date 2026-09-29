"""Text the shell builds while it runs: reported, and refused where it names an
operation a guard checks.

Two ways the shell assembles text this parser cannot read from the command as
written:

* an escape inside ``$'…'`` quoting, which bash decodes into a character (a letter,
  a newline, a byte by its code) before the word exists;
* an expansion (a variable, a substitution, an escape) standing where a git
  SUBCOMMAND goes, so the operation is decided at run time.

THE FIX FOLLOWS THE LINE-CONTINUATION ONE: report, do not model. A built escape
that decodes to anything outside a small formatting set is a BOUNDS-type blind
spot (no segments returned), and ``mention_views`` gains a reading with the escapes
decoded, so each guard's existing mention test and blind-spot refusal apply. A git
subcommand the parse cannot read is refused by the discard guard and the commit
gate, as the push guard already refused it.

Trigger literals are assembled from fragments so this file's own text does not carry
them, per the convention in the other guard suites.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import shell_parse as sp  # noqa: E402

_PY = sys.executable

# ── trigger literals assembled from fragments ──
GIT = "git"
CLEAN = "cl" + "ean"
COMMIT = "com" + "mit"
RM = "r" + "m"
E = "$" + "'"  # opens an ANSI-C string
NL = "\\n"  # backslash-n, as typed inside E…'

_NEUTRAL_ENV = ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")

_DISCARD = _HOOKS_DIR / "git_discard_guard.py"
_PUSH = _HOOKS_DIR / "git_push_guard.py"
_PROTECTED = _HOOKS_DIR / "protected_paths_guard.py"
_DESTRUCTIVE = _HOOKS_DIR / "destructive_command_guard.py"
_FULL_SUITE = _HOOKS_DIR / "full_suite_guard.py"
_TMUX = _HOOKS_DIR / "tmux_kill_server_guard.py"
_CAPPED = _HOOKS_DIR / "capped_read_advisory.py"
_COMMIT_GATE = _WORKTREE / "scripts" / "review_enforcement_commit.py"


def _env(home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _NEUTRAL_ENV if k in os.environ}
    env["HOME"] = str(home)
    env["GENESIS_HOME"] = str(home / ".genesis")
    return env


@pytest.fixture
def sandbox():
    # Outside every repository, so a guard resolving the cwd's repo finds this one.
    with tempfile.TemporaryDirectory(prefix="built-words-") as d:
        root = Path(d)
        home = root / "home"
        (home / "genesis" / "data").mkdir(parents=True)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feature-x", str(repo)], check=True)
        yield home, repo


def _run(script: Path, command: str, home: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_PY, str(script)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_env(home),
        timeout=120,
    )


def _refused(res: subprocess.CompletedProcess) -> bool:
    try:
        out = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    decision = out.get("hookSpecificOutput", {}).get("permissionDecision", "")
    return res.returncode == 2 or decision == "deny"


# ── the parser half ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "a" + NL + "b",  # a newline
        "cl\\x65an",  # a letter by hex
        "\\x3b",  # a separator by hex
        "\\162m",  # a letter by octal
        "\\u0065",  # a letter by code point
        "a\\cJb",  # a control character that is a newline
        "\\x24",  # punctuation: not on the formatting list, so it counts
    ],
    ids=[
        "newline",
        "hex-letter",
        "hex-separator",
        "octal-letter",
        "unicode",
        "control-J",
        "dollar",
    ],
)
def test_an_escape_that_builds_text_is_a_bounds_type_blind_spot(body):
    command = f"echo {E}{body}'"
    segments, blind = sp.analyze_checked(command)
    assert sp.has_built_escape(command)
    assert segments == []
    assert blind is not None and blind.bounds_induced
    assert "decodes" in blind.cause


@pytest.mark.parametrize(
    "command",
    [
        f"printf {E}%s\\t%s' a b",  # layout only
        f"echo {E}plain text'",  # no escape at all
        f"echo {E}a\\\\b'",  # an escaped backslash
        f"echo {E}\\e[1m'",  # a terminal escape
        "echo 'not ANSI-C: \\x65'",  # a plain single-quoted string
    ],
    ids=["tab", "no-escape", "escaped-backslash", "terminal-escape", "plain-quotes"],
)
def test_formatting_escapes_leave_the_command_readable(command):
    assert not sp.has_built_escape(command)
    _segments, blind = sp.analyze_checked(command)
    assert blind is None or blind.cause != sp._BLIND_BUILT_ESCAPE.cause


def test_the_blind_spot_is_in_the_domain():
    assert sp._BLIND_BUILT_ESCAPE in sp._ALL_BLIND_SPOTS


@pytest.mark.parametrize(
    ("text", "decoded"),
    [
        (f"{E}cl\\x65an'", "clean"),
        (f"{E}a" + NL + "b'", "a\nb"),
        (f"{E}\\162m'", "rm"),
        (f"{E}\\u0065'", "e"),
        (f"{E}a\\cJb'", "a\nb"),
        (f"{E}it\\'s'", "it's"),
        (f"{E}\\q'", "\\q"),  # an escape bash does not know keeps its backslash
        ("x " + f"{E}\\x41'" + " y " + f"{E}\\x42'", "x A y B"),
    ],
    ids=["hex", "newline", "octal", "unicode", "control", "quote", "unknown", "two-strings"],
)
def test_decode_ansi_c_matches_bash(text, decoded):
    assert sp.decode_ansi_c(text) == decoded


def test_a_mention_reading_shows_the_decoded_word():
    command = f"{GIT} {E}cl\\x65an' -fd"
    assert sp.mentions(command, f"{GIT} {CLEAN} -fd")
    assert sp.mention_views(command)[0] == command, "widen-only: the raw text comes first"


def test_the_shell_rewrites_before_running_for_both_causes():
    assert sp.rewrites_before_running(f"{GIT} " + "\\\n" + f"{CLEAN} -fd")
    assert sp.rewrites_before_running(f"{GIT} {E}cl\\x65an' -fd")
    assert not sp.rewrites_before_running(f"{GIT} status")


# ── the guards ───────────────────────────────────────────────────────────────────

# (id, guard, plain form, built form). The plain form is asserted refused first, so a
# fixture that never reaches its gate fails loudly instead of passing for the wrong
# reason.
_BUILT = [
    (
        "discard-separator",
        _DISCARD,
        f"bash -c 'true; {GIT} {CLEAN} -fdx'",
        f"bash -c {E}true{NL}{GIT} {CLEAN} -fdx'",
    ),
    ("discard-letter", _DISCARD, f"{GIT} {CLEAN} -fd", f"{GIT} {E}cl\\x65an' -fd"),
    (
        "protected-separator",
        _PROTECTED,
        f"bash -c 'true; {RM} -rf ~/genesis/data'",
        f"bash -c {E}true{NL}{RM} -rf ~/genesis/data'",
    ),
    ("protected-program", _PROTECTED, f"{RM} -rf ~/genesis/data", f"{E}\\x72m' -rf ~/genesis/data"),
    (
        "destructive-separator",
        _DESTRUCTIVE,
        f"bash -c 'true; {RM} -rf ~'",
        f"bash -c {E}true{NL}{RM} -rf ~'",
    ),
    ("destructive-program", _DESTRUCTIVE, f"{RM} -rf ~", f"{E}\\x72m' -rf ~"),
    ("full-suite-separator", _FULL_SUITE, "bash -c 'true; pytest'", f"bash -c {E}true{NL}pytest'"),
    ("commit-letter", _COMMIT_GATE, f"{GIT} {COMMIT} -n -m x", f"{GIT} {E}co\\x6dmit' -n -m x"),
]


@pytest.mark.parametrize(
    ("guard", "plain", "built"), [(g, p, b) for _, g, p, b in _BUILT], ids=[i for i, *_ in _BUILT]
)
def test_text_built_from_an_escape_is_refused_where_its_plain_form_is(sandbox, guard, plain, built):
    home, repo = sandbox
    assert _refused(_run(guard, plain, home, repo)), "control: the plain form must be refused"
    res = _run(guard, built, home, repo)
    assert _refused(res), (res.stdout + res.stderr)[:400]


# A git subcommand the parse cannot read. The push guard refused these already; the
# discard guard and the commit gate did not.
_HIDDEN = [
    ("discard-substitution", _DISCARD, f"{GIT} $(printf 'cl\\x65an') -fd"),
    ("discard-variable", _DISCARD, f"V={CLEAN}; {GIT} $V -fd"),
    ("commit-substitution", _COMMIT_GATE, f"{GIT} $(printf 'co\\x6dmit') -n -m x"),
    ("commit-variable-unnamed", _COMMIT_GATE, f"{GIT} $SUB -n -m x"),
]


@pytest.mark.parametrize(
    ("guard", "command"), [(g, c) for _, g, c in _HIDDEN], ids=[i for i, *_ in _HIDDEN]
)
def test_a_git_subcommand_the_parse_cannot_read_is_refused(sandbox, guard, command):
    home, repo = sandbox
    res = _run(guard, command, home, repo)
    assert _refused(res), (res.stdout + res.stderr)[:400]
    assert "cannot read" in res.stderr, res.stderr


# Controls: text the shell builds that decides nothing, and an expansion in an
# ARGUMENT, run as before in every guard this change touches.
_CONTROLS = [
    f"printf {E}%s\\t%s' a b",
    f"echo {E}hello" + NL + "world'",
    f'{GIT} -C "$DIR" status',
    f"{GIT} log --format={E}%h\\t%s' -3",
    f'{GIT} {COMMIT} -m "$MSG"',
]


@pytest.mark.parametrize(
    "command", _CONTROLS, ids=["tab", "newline-echo", "C-var", "format", "commit-msg-var"]
)
@pytest.mark.parametrize(
    "guard",
    [_DISCARD, _PROTECTED, _DESTRUCTIVE, _FULL_SUITE],
    ids=["discard", "protected", "destructive", "full-suite"],
)
def test_built_text_that_names_nothing_gated_runs(sandbox, guard, command):
    home, repo = sandbox
    res = _run(guard, command, home, repo)
    assert res.returncode == 0, (res.stdout + res.stderr)[:400]


def test_the_commit_gate_leaves_a_git_command_with_a_readable_subcommand_alone(sandbox):
    home, repo = sandbox
    res = _run(_COMMIT_GATE, f'{GIT} -C "$DIR" status', home, repo)
    assert res.returncode == 0, (res.stdout + res.stderr)[:400]


def test_the_tmux_note_reads_a_decoded_word(sandbox):
    home, repo = sandbox
    res = _run(_TMUX, f"tmux {E}\\x6bill-server'", home, repo)
    assert res.returncode == 0
    assert res.stdout.strip(), "the advisory must speak"


def test_the_capped_read_note_reads_a_decoded_word(sandbox):
    """Per command, like a continued command: the keyed once-per-session block is
    for the size bounds, which real commands never reach."""
    home, repo = sandbox
    for _ in range(2):
        res = _run(_CAPPED, f"gh pr {E}\\x6cist'", home, repo)
        assert res.returncode == 0
        assert "could not check the gh read" in res.stdout, res.stdout


# ── the decoder never raises ─────────────────────────────────────────────────────


@pytest.mark.parametrize("head", list("xuUc01234567abefnrtv\\'\"?qzXY8"))
@pytest.mark.parametrize("tail", ["", "g", "0", "41", "4142", "FFFFFFFFF", "?", " "])
def test_the_decoder_never_raises(head, tail):
    """A raise inside the parse reaches guards whose parse-error path fails open.
    MEASURED in review: a bare hex escape crashed it, and one guard exited 1
    (non-blocking) while another swallowed the error and allowed the command."""
    command = f"echo {E}\\{head}{tail}'"
    sp.has_built_escape(command)
    sp.decode_ansi_c(command)
    sp.analyze_checked(command)


@pytest.mark.parametrize(
    ("guard", "command"),
    [
        (_DISCARD, f"{GIT} {CLEAN} -fd {E}\\x'"),
        (_FULL_SUITE, f"pytest {E}\\x'"),
        (_DISCARD, f"{GIT} {CLEAN} -fd {E}\\u'"),
    ],
    ids=["discard-bare-x", "full-suite-bare-x", "discard-bare-u"],
)
def test_an_escape_bash_does_not_recognise_leaves_the_verdict_alone(sandbox, guard, command):
    home, repo = sandbox
    res = _run(guard, command, home, repo)
    assert _refused(res), (res.returncode, (res.stdout + res.stderr)[:400])
    assert "Traceback" not in res.stderr, res.stderr[:400]


@pytest.mark.parametrize("body", ["\\q", "\\x", "\\xg", "\\u", "\\U"])
def test_an_escape_bash_does_not_recognise_is_literal_text(body):
    """bash keeps the backslash (MEASURED), so nothing is built."""
    assert not sp.has_built_escape(f"echo {E}{body}'")
