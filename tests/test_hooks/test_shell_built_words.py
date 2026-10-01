"""Text the shell builds while it runs: reported, and refused where it names an
operation a guard checks.

Two ways the shell assembles text this parser cannot read from the command as
written:

* an escape inside ``$'…'`` quoting, which bash decodes into a character (a letter,
  a newline, a byte by its code) before the word exists;
* an expansion (a variable, a substitution, an escape) standing where a git
  SUBCOMMAND goes, so the operation is decided at run time.

THE FIX FOLLOWS THE LINE-CONTINUATION ONE: report, do not model. A built escape
that decodes to anything outside a small inert set is a BOUNDS-type blind spot (no
segments returned), and ``mention_views`` gains a reading with the escapes decoded,
so each guard's existing mention test and blind-spot refusal apply. A git
subcommand the parse cannot read (a variable, a substitution, a brace expansion) is
refused by the discard guard and the commit gate, as the push guard already refused
it.

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
        "\\x24",  # punctuation: not on the inert list, so it counts
        "a\\tb",  # a tab: a word separator to a nested shell
        "a\\\\b",  # a backslash: escapes the next character in a nested shell
        "it\\'s",  # a quote: opens a string in a nested shell
        '\\"x\\"',  # a double quote, likewise
        "cl\\?an",  # a glob character
        "\\cß",  # a control escape on a non-ASCII character
    ],
    ids=[
        "newline",
        "hex-letter",
        "hex-separator",
        "octal-letter",
        "unicode",
        "control-J",
        "dollar",
        "tab",
        "backslash",
        "single-quote",
        "double-quote",
        "glob",
        "control-non-ascii",
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
        f"echo {E}plain text'",  # no escape at all
        f"echo {E}\\e[1m'",  # a terminal escape
        f"echo {E}a\\ab'",  # a bell
        f"echo {E}a\\fb\\vc\\bd'",  # form feed, vertical tab, backspace
        "echo 'not ANSI-C: \\x65'",  # a plain single-quoted string
    ],
    ids=["no-escape", "terminal-escape", "bell", "ff-vt-bs", "plain-quotes"],
)
def test_inert_escapes_leave_the_command_readable(command):
    assert not sp.has_built_escape(command)
    _segments, blind = sp.analyze_checked(command)
    assert blind is None or blind.cause != sp._BLIND_BUILT_ESCAPE.cause


def test_the_inert_set_holds_no_shell_syntax():
    """A character bash's grammar gives meaning to (a separator, a quote, an escape,
    a glob, an expansion) changes what a NESTED shell runs, so it cannot be inert.
    Found in review: tab and the quotes were once on this list."""
    syntax = set(" \t\n|&;()<>'\"\\`$*?[]{}~#=%!")
    assert not sp._ANSI_C_INERT & syntax
    assert all(len(ch) == 1 and not ch.isprintable() for ch in sp._ANSI_C_INERT)


@pytest.mark.parametrize("char", sorted(sp._ANSI_C_INERT), ids=repr)
def test_an_inert_escape_keeps_a_nested_word_whole(tmp_path, char):
    """MEASURED on bash itself, not reasoned: with the character between two words in
    a script handed to ``bash -c``, the two words stay one word, so the second
    never becomes a command of its own."""
    shim = tmp_path / "bin"
    shim.mkdir()
    ran = tmp_path / "ran"
    (shim / GIT).write_text(f'#!/bin/sh\necho "$@" > {ran}\n')
    (shim / GIT).chmod(0o755)
    escape = {"\a": "\\a", "\b": "\\b", "\x1b": "\\e", "\f": "\\f", "\v": "\\v"}[char]
    script = f"bash -c {E}{GIT}{escape}{CLEAN} -n'"
    subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{shim}:/usr/bin:/bin"},
        capture_output=True,
        timeout=30,
    )
    assert not ran.exists(), "the escape split the word, so it is not inert"


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
        "discard-nested-tab",
        _DISCARD,
        f"bash -c '{GIT} {CLEAN} -fdx'",
        f"bash -c {E}{GIT}\\t{CLEAN} -fdx'",
    ),
    (
        "discard-nested-double-quote",
        _DISCARD,
        f"bash -c 'echo x; {GIT} {CLEAN} -fd'",
        f'bash -c {E}echo \\"x\\"; {GIT} {CLEAN} -fd\'',
    ),
    (
        "discard-nested-single-quote",
        _DISCARD,
        f"bash -c 'echo x; {GIT} {CLEAN} -fd'",
        f"bash -c {E}echo \\'x\\'; {GIT} {CLEAN} -fd'",
    ),
    (
        "discard-control-non-ascii",
        _DISCARD,
        f"true; {GIT} {CLEAN} -fd",
        f"true {E}\\cß'; {GIT} {CLEAN} -fd",
    ),
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
    ("discard-brace-range", _DISCARD, f"{GIT} cl{{e..e}}an -fd"),
    ("discard-brace-range-late", _DISCARD, f"{GIT} cle{{a..a}}n -fd"),
    ("discard-brace-list", _DISCARD, f"{GIT} cl{{e,}}an -fd"),
    ("commit-substitution", _COMMIT_GATE, f"{GIT} $(printf 'co\\x6dmit') -n -m x"),
    ("commit-variable-unnamed", _COMMIT_GATE, f"{GIT} $SUB -n -m x"),
    ("commit-brace-range", _COMMIT_GATE, f"{GIT} com{{m..m}}it -n -m x"),
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


@pytest.mark.parametrize("head", list("xuUc01234567abefnrtv\\'\"?qzXYß8"))
@pytest.mark.parametrize(
    "tail", ["", "g", "0", "41", "4142", "FFFFFFFFF", "?", " ", "ß", "ﬀ", "ŉ", "é", "😀", "\ud800"]
)
def test_the_decoder_never_raises(head, tail):
    """A raise inside the parse reaches guards whose parse-error path fails open.
    MEASURED in review, twice: a bare hex escape crashed it, then a control escape on
    a character whose upper case is two characters. Each time one guard exited 1
    (non-blocking). The tails now include non-ASCII characters: the first grid held
    only ASCII, which is why it could not find the second."""
    command = f"echo {E}\\{head}{tail}'"
    sp.has_built_escape(command)
    sp.decode_ansi_c(command)
    sp.mention_views(command)
    sp.analyze_checked(command)


def test_an_escape_the_decoder_cannot_place_counts_as_built(monkeypatch):
    """The ONE decoding chokepoint stays total even if a decode rule raises, and what
    it could not decode is built text, so the refusal still applies."""

    def boom(_escape):
        raise ValueError("a decode rule that raises")

    monkeypatch.setattr(sp, "_ansi_c_char", boom)
    command = f"{GIT} {E}cl\\x65an' -fd"
    assert sp.has_built_escape(command)
    assert sp._UNDECODABLE in sp.decode_ansi_c(command)
    sp.mention_views(command)
    _segments, blind = sp.analyze_checked(command)
    assert blind is not None and blind.cause == sp._BLIND_BUILT_ESCAPE.cause


def test_every_control_escape_decodes_to_one_character():
    """Over EVERY code point, not a sample: ``\\c`` + any character is one control
    character, so it is always built text and never raises."""
    bad = []
    for cp in range(0x110000):
        try:
            out = sp._ansi_c_char("c" + chr(cp))
        except Exception as exc:  # noqa: BLE001 — the point is to record every raise
            bad.append((hex(cp), type(exc).__name__))
            continue
        if len(out) != 1 or ord(out) > 0x7F:
            bad.append((hex(cp), repr(out)))
    assert not bad, bad[:10]


@pytest.mark.parametrize("char", ["a", "Z", "[", "?", "ß", "é", "ﬀ", "😀"])
def test_a_control_escape_decodes_like_bash(char):
    """bash masks the FIRST BYTE of the character; compared against bash itself."""
    out = subprocess.run(
        ["bash", "-c", f"printf %s {E}\\c{char}'"], capture_output=True, timeout=30
    ).stdout
    assert out[:1] == sp.decode_ansi_c(f"{E}\\c{char}'").encode("latin-1")[:1]


@pytest.mark.parametrize(
    "token",
    [
        "$V",
        "${V}",
        "$(printf x)",
        "`printf x`",
        "cl{e..e}an",
        "cle{a..a}n",
        "cl{e,}an",
        "{a,b}",
        "x" * (sp._MAX_VERB_WORD_CHARS + 1),
    ],
    ids=[
        "var",
        "braced-var",
        "subst",
        "backtick",
        "range",
        "range-late",
        "list",
        "bare-list",
        "long",
    ],
)
def test_may_build_words_sees_every_word_the_parser_cannot_read(token):
    """The discard guard's early exit asks :func:`may_build_words` whether to let the
    parse look. It must hold for every word the parser itself would refuse to read;
    a hand-kept list missed brace expansion in review."""
    assert not sp._word_is_literal(token), "fixture: the parser must refuse to read it"
    assert sp.may_build_words(f"{GIT} {token} -fd")


def test_may_build_words_leaves_ordinary_commands_on_the_fast_path():
    for command in (f"{GIT} status", f"{GIT} log -3 --oneline", "ls -la"):
        assert not sp.may_build_words(command)


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
