"""A program word in bash's locale-translation quoting is reported, not resolved.

Bash reads a dollar sign directly before a double-quoted string as a string to
translate, and with no message catalog it runs the text as written. shlex knows
no such form: it removes the quotes and keeps the dollar sign, so the parser
names a program one character off from the one bash runs. In VERB position that
was already reported (``test_guard_ansic_fail_closed``, the ``locale`` rows). In
PROGRAM position it was not, so the guards looked for git or gh, found neither,
and reported nothing.

The parser now reports such a segment as its own blind spot and records the
program the untranslated reading runs. It does not resolve the word: a
translation is possible, so the reading is a candidate, never the executable.

One level down is the same defect: a shell handed its script in that quoting, or
the ``-c`` that selects it (``sh -c $"git push"``, ``bash $"-c" '...'``). That is
reported as its own cause, and the untranslated script is kept as a candidate
that the guards mention-scan for git and gh; it is never parsed.

Every command here is generated from fragments, run under bash with recording
stand-ins for git and gh (so nothing is pushed or merged), and run through the
real push guard as a subprocess. The verdict reads the exit status AND the JSON
payload: an ask is exit 0 with a payload, which an exit-code probe reads as an
allow.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

import shell_parse as sp  # noqa: E402

_PUSH_GUARD = _HOOKS / "git_push_guard.py"
_DISCARD_GUARD = _HOOKS / "git_discard_guard.py"
_COMMIT_GUARD = _HOOKS.parent / "review_enforcement_commit.py"
_DESTRUCTIVE_GUARD = _HOOKS / "destructive_command_guard.py"

GIT = "git"
PUSH = "pu" + "sh"
MERGE = "mer" + "ge"
FORCE = "--" + "for" + "ce"

#: (program word, verb word, the words between them, the words after the verb)
_OPERATIONS = {
    "git": (GIT, PUSH, (), ("origin", "main")),
    "gh": ("gh", MERGE, ("pr",), ("5",)),
}
_RANK = {"allow": 0, "ask": 1, "block": 2}
_MODES = {"interactive": None, "dispatched": "1"}


def _locale(word: str) -> str:
    return "$" + '"' + word + '"'


#: One word, five ways. Bash runs the same word for all of them (the oracle below
#: asserts that per cell rather than trusting this table).
_SPELLINGS = {
    "plain": lambda word: word,
    "ansi_c": lambda word: "$'" + word + "'",
    "locale": _locale,
    "split": lambda word: word[0] + '"' + word[1:] + '"',
    "backslash": lambda word: "\\" + word,
}


def _wanted(program: str) -> tuple[str, ...]:
    exe, verb, between, after = _OPERATIONS[program]
    return (exe, *between, verb, *after)


def _command(program: str, position: str, spelling: str) -> str:
    exe, verb, between, after = _OPERATIONS[program]
    spell = _SPELLINGS[spelling]
    exe = spell(exe) if position == "program" else exe
    verb = spell(verb) if position == "verb" else verb
    return " ".join((exe, *between, verb, *after))


_SHIM = (
    '#!/bin/sh\n{ printf "%s\\037" "${0##*/}" "$@"; printf "\\036"; } >> "$ORACLE_LOG"\nexit 0\n'
)
_NEUTRAL_ENV = ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


class _Sandbox:
    """A throwaway home, an unpublished repository, and stand-ins for git and gh."""

    def __init__(self, holder: tempfile.TemporaryDirectory) -> None:
        self._holder = holder  # removed with this object, at interpreter exit
        root = Path(holder.name)
        self.home = root / "home"
        self.repo = root / "repo"
        self.work = root / "work"
        self.shims = root / "bin"
        for directory in (self.home, self.repo, self.work, self.shims):
            directory.mkdir()
        # `rm` too, so a removal the oracle builds is recorded rather than run.
        for name in ("git", "gh", "rm"):
            shim = self.shims / name
            shim.write_text(_SHIM, encoding="utf-8")
            shim.chmod(0o755)
        self.log = root / "oracle.log"
        # A branch its remote has never seen, on a host that cannot resolve: a
        # plain push is then a live, gated operation with no network behind it.
        for step in (
            ("init", "-q", "-b", "feature-x"),
            (
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            ),
            ("remote", "add", "origin", "https://example.invalid/testowner/testrepo.git"),
        ):
            done = subprocess.run(
                ["git", *step], cwd=self.repo, env=self._env(None), capture_output=True, text=True
            )
            assert done.returncode == 0, f"fixture step {step!r} failed: {done.stderr}"

    def _env(self, dispatched: str | None) -> dict[str, str]:
        # An allowlist: nothing ambient reaches the guard unless named here, and the
        # home is the sandbox, so no real config is read and no real store written.
        env = {k: os.environ[k] for k in _NEUTRAL_ENV if k in os.environ}
        env["HOME"] = str(self.home)
        env["GENESIS_HOME"] = str(self.home / ".genesis")
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        if dispatched is not None:
            env["GENESIS_CC_SESSION"] = dispatched
        return env

    def ran(self, command: str) -> list[tuple[str, ...]]:
        """The argv of every stand-in bash executed for ``command``."""
        self.log.write_text("", encoding="utf-8")
        path = f"{self.shims}:/usr/bin:/bin"
        # Two layers keep the oracle harmless: the home is the sandbox, and the
        # stand-ins must be what bash resolves before anything runs.
        for name in ("git", "gh", "rm"):
            assert shutil.which(name, path=path) == str(self.shims / name)
        subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", command],
            cwd=self.work,
            env={"PATH": path, "HOME": str(self.work), "ORACLE_LOG": str(self.log), "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        data = self.log.read_text(encoding="utf-8")
        return [tuple(rec.split("\x1f")[:-1]) for rec in data.split("\x1e") if rec]

    def guard(self, command: str, mode: str, script: Path | None = None) -> tuple[str, str]:
        """``(allow | ask | block, the guard's message)`` for one command."""
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.repo)}
        done = subprocess.run(
            [sys.executable, str(script or _PUSH_GUARD)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=self._env(_MODES[mode]),
            timeout=120,
        )
        if done.returncode == 2:
            return "block", done.stderr
        # Any other non-zero status is a crash, which the harness reads as an allow.
        assert done.returncode == 0, f"the guard crashed ({done.returncode}): {done.stderr}"
        if not done.stdout.strip():
            return "allow", ""
        hook = json.loads(done.stdout).get("hookSpecificOutput", {})
        decision = hook.get("permissionDecision")
        reason = hook.get("permissionDecisionReason", "")
        if decision == "deny":
            return "block", reason
        return ("ask", reason) if decision == "ask" else ("allow", reason)


@cache
def _sandbox() -> _Sandbox:
    # Not tmp_path: the root must sit outside every repository, so a guard that
    # resolves the cwd's repository can only find the one built here. Kept for the
    # module, since the guard runs are the cost.
    return _Sandbox(tempfile.TemporaryDirectory(prefix="locale-program-"))


@pytest.fixture(scope="module")
def sandbox() -> _Sandbox:
    if shutil.which("bash") is None:
        pytest.skip("the oracle needs bash")
    return _sandbox()


@cache
def _guard(command: str, mode: str) -> tuple[str, str]:
    return _sandbox().guard(command, mode)


# ── the harness itself ────────────────────────────────────────────────────────


def test_the_harness_tells_the_three_outcomes_apart(sandbox):
    """An ask is exit 0, like an allow. Prove this probe separates all three on
    known inputs before any cell below is believed."""
    plain_push = _command("git", "program", "plain")
    assert _guard("ls -la", "interactive")[0] == "allow"
    assert _guard(plain_push, "interactive")[0] == "ask"
    assert _guard(plain_push, "dispatched")[0] == "block"


# ── the generated matrix ──────────────────────────────────────────────────────

_MATRIX = [
    pytest.param(position, spelling, program, mode, id=f"{position}-{spelling}-{program}-{mode}")
    for position in ("program", "verb")
    for spelling in _SPELLINGS
    for program in _OPERATIONS
    for mode in _MODES
]


@pytest.mark.parametrize(("position", "spelling", "program", "mode"), _MATRIX)
def test_no_spelling_of_a_gated_operation_is_silently_allowed(
    position, spelling, program, mode, sandbox
):
    """{program, verb} x {five spellings} x {git, gh} x {interactive, dispatched}.

    The invariant per cell: bash runs the gated operation, so the guard must not
    allow it in silence. A spelling the parser resolves is at least as strict as
    the plain one; a locale-quoted word is refused in both modes, with the cause
    that names its own remedy.
    """
    command = _command(program, position, spelling)
    assert sandbox.ran(command) == [_wanted(program)], (
        "oracle: this cell does not build the operation it claims to"
    )
    verdict, message = _guard(command, mode)
    assert verdict != "allow", (
        f"bash runs {' '.join(_wanted(program))} and the guard allowed it without a word"
    )
    if spelling != "locale":
        plain = _guard(_command(program, position, "plain"), mode)[0]
        assert _RANK[verdict] >= _RANK[plain], (
            f"this spelling is answered {verdict!r}, weaker than the plain one's {plain!r}"
        )
        return
    assert verdict == "block", f"a locale-quoted word is refused, not asked about: {message}"
    cause = (
        sp._BLIND_LOCALE_PROGRAM.cause if position == "program" else sp._BLIND_UNRESOLVED_VERB.cause
    )
    assert cause in message, f"the refusal names the wrong cause:\n{message}"


# The same class, in the places a program word can sit. Each is (id, command, the
# program whose operation bash runs).
_PROGRAM_SPELLINGS = [
    ("head_fragment", f"{_locale('g')}it {PUSH} origin main", "git"),
    ("tail_fragment", f"g{_locale('it')} {PUSH} origin main", "git"),
    ("middle_fragment", f"gi{_locale('t')} {PUSH} origin main", "git"),
    ("empty_fragment", f"{_locale('')}{GIT} {PUSH} origin main", "git"),
    ("after_a_builtin_wrapper", f"command {_locale(GIT)} {PUSH} origin main", "git"),
    ("after_env", f"env {_locale(GIT)} {PUSH} origin main", "git"),
    ("in_a_nested_shell", "bash -c " + shlex.quote(f"{_locale(GIT)} {PUSH} origin main"), "git"),
    ("in_a_command_substitution", f"echo $({_locale(GIT)} {PUSH} origin main)", "git"),
    ("in_backticks", f"echo `{_locale(GIT)} {PUSH} origin main`", "git"),
    ("the_wrapper_word", f"{_locale('command')} {GIT} {PUSH} origin main", "git"),
    ("a_wrapper_option", f"timeout {_locale('-k')} 5 10 {GIT} {PUSH} origin main", "git"),
    ("gh_head_fragment", f"{_locale('g')}h pr {MERGE} 5", "gh"),
]


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("command", "program"),
    [pytest.param(c, p, id=i) for i, c, p in _PROGRAM_SPELLINGS],
)
def test_a_locale_quoted_program_word_is_refused(command, program, mode, sandbox):
    assert sandbox.ran(command) == [_wanted(program)], "oracle: bash must run the operation"
    verdict, message = _guard(command, mode)
    assert verdict == "block", f"bash runs the operation and the guard answered {verdict!r}"
    assert sp._BLIND_LOCALE_PROGRAM.cause in message, message


@pytest.mark.parametrize("mode", _MODES)
def test_a_locale_quoted_program_behind_a_directory_is_refused(mode, sandbox):
    # The stand-in's own directory: the path differs per run, so it is built here
    # and never becomes a test id.
    command = f"{sandbox.shims}/{_locale(GIT)} {PUSH} origin main"
    assert sandbox.ran(command) == [_wanted("git")]
    assert _guard(command, mode)[0] == "block"


def test_a_locale_quoted_push_cannot_ride_a_visible_one(sandbox):
    """A push that parses must not stand the net down for one that does not.

    On the tree before this change the pair was ASKED about interactively, and the
    prompt named only the visible push. A dispatched session was refused either
    way, since nobody can answer its ask, so that cell is a lock, not the witness.
    """
    visible = f"{GIT} {PUSH}"
    assert _guard(visible, "interactive")[0] == "ask", (
        "the visible push must be live and merely asked about, or the pair below "
        "could be refused for a reason that has nothing to do with the other one"
    )
    command = f"{_locale(GIT)} {PUSH} {FORCE} origin main && {visible}"
    assert sorted(sandbox.ran(command)) == sorted(
        [(GIT, PUSH), (GIT, PUSH, FORCE, "origin", "main")]
    )
    verdict, message = _guard(command, "interactive")
    assert verdict == "block", f"answered {verdict!r}: {message}"
    assert sp._BLIND_LOCALE_PROGRAM.cause in message, message
    assert _guard(command, "dispatched")[0] == "block"


@pytest.mark.parametrize("mode", _MODES)
def test_a_continuation_does_not_change_the_answer(mode, sandbox):
    """A bounds-type blind spot withholds the segments and the fact on them, so the
    guard recovers it; the command names no gated word, so nothing else would."""
    one_line = f"{_locale(GIT)} status -s"
    continued = f"{_locale(GIT)} status \\\n -s"
    assert sandbox.ran(continued) == sandbox.ran(one_line) == [(GIT, "status", "-s")]
    assert _guard(one_line, mode)[0] == "block"
    assert _guard(continued, mode)[0] == "block"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("script", "tail", "ran"),
    [
        pytest.param(_DISCARD_GUARD, "clean -fd", (GIT, "clean", "-fd"), id="discard"),
        pytest.param(
            _COMMIT_GUARD, "commit -n -m x", (GIT, "commit", "-n", "-m", "x"), id="commit"
        ),
    ],
)
def test_the_guards_that_share_the_recovered_fact_refuse_it_too(script, tail, ran, mode, sandbox):
    """`unresolved_verb_programs` is how the commit and discard guards learn that a
    git operation they cannot name may run. A program that is not git stays theirs
    to ignore."""
    command = f"{_locale(GIT)} {tail}"
    assert sandbox.ran(command) == [ran], "oracle: bash must run the operation"
    assert sandbox.guard(f"{GIT} {tail}", mode, script)[0] == "block", "control: the plain form"
    assert sandbox.guard(command, mode, script)[0] == "block"
    assert sandbox.guard(f"{_locale('ls')} -la", mode, script)[0] == "allow"


# ── a shell's script, and the -c that selects it ──────────────────────────────
#
# The same quoting one level down: a shell handed its script in `$"..."`, or the
# `-c` that selects the script written that way. shlex keeps the dollar sign, so
# the parse finds `$git push` (a program held in a variable, which is not reported)
# or no script at all, while bash runs the plain text. Each is (id, command, the
# argv bash runs). `sh` is whatever /bin/sh is: the locale quoting is the OUTER
# shell's, so the inner shell receives plain text either way.

_SCRIPT_SPELLINGS = [
    ("sh_c", f"sh -c {_locale(f'{GIT} {PUSH} origin main')}", _wanted("git")),
    ("bash_c", f"bash -c {_locale(f'{GIT} {PUSH} origin main')}", _wanted("git")),
    ("bundled_lc", f"bash -lc {_locale(f'{GIT} {PUSH} origin main')}", _wanted("git")),
    ("after_a_pipe", f"echo hi | bash -c {_locale(f'{GIT} {PUSH} origin main')}", _wanted("git")),
    (
        "behind_xargs",
        f"echo x | xargs -I{{}} sh -c {_locale(f'{GIT} {PUSH} origin main')}",
        _wanted("git"),
    ),
    ("the_c_flag", f"bash {_locale('-c')} '{GIT} {PUSH} origin main'", _wanted("git")),
    (
        "flag_and_script",
        f"sh {_locale('-c')} {_locale(f'{GIT} {PUSH} origin main')}",
        _wanted("git"),
    ),
    ("a_fragment", f"sh -c {_locale(f'{GIT} pu')}'sh origin main'", _wanted("git")),
    ("gh", f"sh -c {_locale(f'gh pr {MERGE} 5')}", _wanted("gh")),
    (
        "in_a_nested_shell",
        "bash -c " + shlex.quote(f"sh -c {_locale(f'{GIT} {PUSH} origin main')}"),
        _wanted("git"),
    ),
    (
        "after_another_command",
        # Not `ls; git push` inside the quoting: there the dollar sign stays on `ls`,
        # the push parses in the script as written, and is refused as a visible one.
        f"cd . && sh -c {_locale(f'{GIT} {PUSH} origin main')}",
        _wanted("git"),
    ),
]


def _script_cause() -> str | None:
    # Read lazily so a tree without the cause fails on the verdict, not on import.
    spot = getattr(sp, "_BLIND_LOCALE_SCRIPT", None)
    return spot.cause if spot is not None else None


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("command", "wanted"),
    [pytest.param(c, w, id=i) for i, c, w in _SCRIPT_SPELLINGS],
)
def test_a_locale_quoted_script_is_refused(command, wanted, mode, sandbox):
    assert wanted in sandbox.ran(command), "oracle: bash must run the operation"
    verdict, message = _guard(command, mode)
    assert verdict == "block", f"bash runs {' '.join(wanted)} and the guard answered {verdict!r}"
    cause = _script_cause()
    assert cause is not None and cause in message, f"the refusal names the wrong cause:\n{message}"


def test_a_locale_quoted_script_cannot_ride_a_visible_push(sandbox):
    """The per-segment fact, like the program word's: a push that parses must not
    stand the net down for one the guard cannot read."""
    visible = f"{GIT} {PUSH}"
    assert _guard(visible, "interactive")[0] == "ask", "control: the visible push is only asked"
    command = f"sh -c {_locale(f'{GIT} {PUSH} {FORCE} origin main')} && {visible}"
    assert (GIT, PUSH, FORCE, "origin", "main") in sandbox.ran(command)
    verdict, message = _guard(command, "interactive")
    assert verdict == "block", f"answered {verdict!r}: {message}"


@pytest.mark.parametrize("mode", _MODES)
def test_a_locale_quoted_shell_program_cannot_ride_a_visible_push(mode, sandbox):
    """`$"bash" -c '...'`: the program word is the locale-quoted one, so the parse
    never opens the script. The script's candidate reading still counts."""
    command = f"{_locale('bash')} -c '{GIT} {PUSH} {FORCE} origin main' && {GIT} {PUSH}"
    assert (GIT, PUSH, FORCE, "origin", "main") in sandbox.ran(command)
    assert _guard(command, mode)[0] == "block"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("script", "tail", "ran"),
    [
        pytest.param(_DISCARD_GUARD, "clean -fd", (GIT, "clean", "-fd"), id="discard"),
        pytest.param(
            _COMMIT_GUARD, "commit -n -m x", (GIT, "commit", "-n", "-m", "x"), id="commit"
        ),
    ],
)
def test_the_sibling_guards_refuse_a_locale_quoted_script(script, tail, ran, mode, sandbox):
    command = f"sh -c {_locale(f'{GIT} {tail}')}"
    assert sandbox.ran(command) == [ran], "oracle: bash must run the operation"
    assert sandbox.guard(f'sh -c "{GIT} {tail}"', mode, script)[0] == "block", (
        "control: the plain form"
    )
    assert sandbox.guard(command, mode, script)[0] == "block"
    assert sandbox.guard(f"sh -c {_locale('echo hi')}", mode, script)[0] == "allow"


# A locale-quoted script that runs nothing gated: the parser reports it, and the
# guards, whose business it is not, allow it exactly as they allow the plain form.
_HARMLESS_SCRIPTS = [
    ("sh_c_echo", f"sh -c {_locale('echo hi')}", 'sh -c "echo hi"'),
    ("bash_c_echo", f"bash -c {_locale('echo hi')}", 'bash -c "echo hi"'),
    ("the_c_flag_echo", f"bash {_locale('-c')} 'echo hi'", "bash -c 'echo hi'"),
    ("program_ls", f"{_locale('ls')} -la", "ls -la"),
]


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "guard", [_PUSH_GUARD, _DISCARD_GUARD, _COMMIT_GUARD], ids=lambda p: p.stem
)
@pytest.mark.parametrize(
    ("command", "plain"), [pytest.param(c, p, id=i) for i, c, p in _HARMLESS_SCRIPTS]
)
def test_a_locale_quoted_script_that_runs_nothing_gated_is_allowed(
    command, plain, guard, mode, sandbox
):
    assert sandbox.ran(command) == sandbox.ran(plain) == [], "oracle: nothing gated runs"
    assert sandbox.guard(plain, mode, guard)[0] == "allow", "control: the plain form"
    assert sandbox.guard(command, mode, guard)[0] == "allow"


@pytest.mark.parametrize(
    ("command", "script"),
    [
        pytest.param(c, s, id=i)
        for i, c, s in [
            ("sh_c", f"sh -c {_locale(f'{GIT} {PUSH} origin main')}", f"{GIT} {PUSH} origin main"),
            ("the_c_flag", f"bash {_locale('-c')} '{GIT} {PUSH}'", f"{GIT} {PUSH}"),
            ("behind_xargs", f"xargs -I{{}} sh -c {_locale('gh pr list')}", "gh pr list"),
            ("a_fragment", f"sh -c {_locale(f'{GIT} pu')}'sh'", f"{GIT} {PUSH}"),
            ("shell_program", f"{_locale('bash')} -c '{GIT} {PUSH}'", f"{GIT} {PUSH}"),
        ]
    ],
)
def test_the_parser_reports_a_locale_quoted_script_and_does_not_parse_it(command, script):
    segments, blind = sp.analyze_checked(command)
    assert blind is not None, (
        "bash runs a script the parse never read, and the parse reported nothing"
    )
    assert not blind.bounds_induced and segments, "the segments are complete and returned"
    assert script in {getattr(s, "locale_script", "") for s in segments}
    # A candidate, never parsed into segments of its own.
    words = tuple(shlex.split(script))
    assert not any(tuple(s.argv[: len(words)]) == words for s in segments)
    assert {"git", "gh"} & sp.unresolved_verb_programs(command)


def test_the_script_cause_has_its_own_remedy():
    spot = getattr(sp, "_BLIND_LOCALE_SCRIPT", None)
    assert spot is not None and spot in sp._ALL_BLIND_SPOTS
    assert spot.bounds_induced is False
    assert spot.cause not in (sp._BLIND_LOCALE_PROGRAM.cause, sp._BLIND_UNRESOLVED_VERB.cause)
    assert "plain quotes" in spot.hint and "-c" in spot.hint
    command = f"sh -c {_locale(f'{GIT} {PUSH}')}"
    assert sp.analyze_checked(command)[1] is spot
    # The program word is reported first when both are locale-quoted.
    both = f"{_locale('sh')} -c {_locale(f'{GIT} {PUSH}')}"
    assert sp.analyze_checked(both)[1] is sp._BLIND_LOCALE_PROGRAM


# ── a body the parser rebuilds: case arms, function bodies, coproc ────────────
#
# The parser reads these bodies from tokens it joins back into text, and by then
# shlex has already turned `$"git"` into `$git`, so the inner parse never saw the
# quoting, and the outer segment resolves to the construct itself (`case`, `f()`,
# `coproc`), which the second reading leaves where it was. Each is (id, command,
# the same command in plain quotes, an argv the oracle must see bash run, or None
# where bash rejects the form and parity with the plain spelling is what is owed).

_EMBEDDED = [
    (
        "case_arm",
        f"case x in x) {_locale(GIT)} {PUSH};; esac",
        f"case x in x) {GIT} {PUSH};; esac",
        (GIT, PUSH),
    ),
    (
        "case_paren_arm",
        f"case x in (x) {_locale(GIT)} {PUSH};; esac",
        f"case x in (x) {GIT} {PUSH};; esac",
        (GIT, PUSH),
    ),
    (
        "case_arm_script",
        f"case x in x) sh -c {_locale(f'{GIT} {PUSH}')};; esac",
        f'case x in x) sh -c "{GIT} {PUSH}";; esac',
        (GIT, PUSH),
    ),
    (
        "function_parens",
        f"f() {{ {_locale(GIT)} {PUSH}; }}; f",
        f"f() {{ {GIT} {PUSH}; }}; f",
        (GIT, PUSH),
    ),
    (
        "function_keyword",
        f"function f {{ {_locale(GIT)} {PUSH}; }}; f",
        f"function f {{ {GIT} {PUSH}; }}; f",
        (GIT, PUSH),
    ),
    ("bare_pattern", f"a) {_locale(GIT)} {PUSH}", f"a) {GIT} {PUSH}", None),
    ("coproc", f"coproc {_locale(GIT)} {PUSH}", f"coproc {GIT} {PUSH}", (GIT, PUSH)),
    (
        "coproc_in_a_case_arm",
        f"case x in x) coproc {_locale(GIT)} {PUSH};; esac",
        f"case x in x) coproc {GIT} {PUSH};; esac",
        (GIT, PUSH),
    ),
    (
        "gh_in_a_function",
        f"f() {{ {_locale('gh')} pr {MERGE} 5; }}; f",
        f"f() {{ gh pr {MERGE} 5; }}; f",
        ("gh", "pr", MERGE, "5"),
    ),
]


def _locale_causes() -> tuple[str, ...]:
    return tuple(
        spot.cause
        for spot in (sp._BLIND_LOCALE_PROGRAM, getattr(sp, "_BLIND_LOCALE_SCRIPT", None))
        if spot is not None
    )


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("command", "plain", "ran"), [pytest.param(c, p, r, id=i) for i, c, p, r in _EMBEDDED]
)
def test_a_locale_quoted_word_in_a_rebuilt_body_is_refused(command, plain, ran, mode, sandbox):
    if ran is not None:
        # `wait`, so a coprocess has run before the log is read.
        assert ran in sandbox.ran(f"{command}; wait"), "oracle: bash must run the operation"
        assert ran in sandbox.ran(f"{plain}; wait"), "oracle: and so must the plain form"
    assert _guard(plain, mode)[0] != "allow", "control: the plain spelling is not allowed"
    verdict, message = _guard(command, mode)
    assert verdict == "block", f"the plain form is refused or asked; this was {verdict!r}"
    assert any(cause in message for cause in _locale_causes()), (
        f"the refusal names the wrong cause:\n{message}"
    )


@pytest.mark.parametrize(
    ("command", "program"),
    [pytest.param(c, r[0], id=i) for i, c, _p, r in _EMBEDDED if r is not None]
    + [pytest.param(_EMBEDDED[5][1], GIT, id="bare_pattern")],
)
def test_the_parser_reports_a_rebuilt_body_and_does_not_resolve_it(command, program):
    segments, blind = sp.analyze_checked(command)
    assert blind in (sp._BLIND_LOCALE_PROGRAM, getattr(sp, "_BLIND_LOCALE_SCRIPT", None)), (
        f"reported as {blind!r}"
    )
    assert not blind.bounds_induced and segments
    assert program in set().union(*(sp.locale_candidates(s) for s in segments))
    assert program in sp.unresolved_verb_programs(command)
    # Reported, never decoded: no segment claims the program as its executable.
    assert not any(s.exe == program for s in segments)


#: A body wrapped three ways, for the guards that refuse a git or rm operation.
_EMBEDDERS = {
    "case_arm": lambda body: f"case x in x) {body};; esac",
    "function": lambda body: f"f() {{ {body}; }}; f",
    "coproc": lambda body: f"coproc {body}",
}


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("form", _EMBEDDERS)
@pytest.mark.parametrize(
    ("script", "tail", "ran"),
    [
        pytest.param(_DISCARD_GUARD, "clean -fd", (GIT, "clean", "-fd"), id="discard"),
        pytest.param(
            _COMMIT_GUARD, "commit -n -m x", (GIT, "commit", "-n", "-m", "x"), id="commit"
        ),
    ],
)
def test_the_sibling_guards_refuse_a_rebuilt_body(script, tail, ran, form, mode, sandbox):
    wrap = _EMBEDDERS[form]
    command = wrap(f"{_locale(GIT)} {tail}")
    assert ran in sandbox.ran(f"{command}; wait"), "oracle: bash must run the operation"
    assert sandbox.guard(wrap(f"{GIT} {tail}"), mode, script)[0] == "block", (
        "control: the plain form"
    )
    assert sandbox.guard(command, mode, script)[0] == "block"


_RM_TARGET = "/x-nonexistent"
#: (id, command, the same command in plain quotes). The removal targets a shallow
#: path, which the guard refuses in plain spelling; the oracle's `rm` is a stand-in.
_LOCALE_REMOVALS = [
    (form, wrap(f"{_locale('rm')} -rf {_RM_TARGET}"), wrap(f"rm -rf {_RM_TARGET}"))
    for form, wrap in _EMBEDDERS.items()
] + [
    (
        "shell_script",
        f"bash -c {_locale(f'rm -rf {_RM_TARGET}')}",
        f'bash -c "rm -rf {_RM_TARGET}"',
    ),
]


@pytest.mark.parametrize(
    ("command", "plain"), [pytest.param(c, p, id=i) for i, c, p in _LOCALE_REMOVALS]
)
def test_the_destructive_guard_refuses_a_locale_quoted_removal(command, plain, sandbox):
    assert ("rm", "-rf", _RM_TARGET) in sandbox.ran(f"{command}; wait"), (
        "oracle: bash must run the removal"
    )
    assert sandbox.guard(plain, "interactive", _DESTRUCTIVE_GUARD)[0] == "block", (
        "control: the plain form"
    )
    assert sandbox.guard(command, "interactive", _DESTRUCTIVE_GUARD)[0] == "block"


def test_the_discard_refusal_names_locale_quoting_and_its_remedy(sandbox):
    """Wording only: the verdict is the same block, and the message says what to
    change, as the push and commit guards' do."""
    for command in (
        f"{_locale(GIT)} clean -fd",
        f"sh -c {_locale(f'{GIT} clean -fd')}",
        f"case x in x) {_locale(GIT)} clean -fd;; esac",
    ):
        verdict, message = sandbox.guard(command, "interactive", _DISCARD_GUARD)
        assert verdict == "block", command
        assert "locale quoting" in message and "plain" in message, message


# Locale quoting in an ARGUMENT inside a rebuilt body, and a body whose locale-quoted
# script runs nothing gated: each answered as its plain spelling is.
_EMBEDDED_CONTROLS = [
    ("case_echo", f"case x in x) echo {_locale('hi')};; esac", 'case x in x) echo "hi";; esac'),
    ("function_echo", f"f() {{ echo {_locale('hi')}; }}; f", 'f() { echo "hi"; }; f'),
    ("coproc_echo", f"coproc echo {_locale('hi')}", 'coproc echo "hi"'),
    (
        "case_commit_message",
        f"case x in x) {GIT} commit -m {_locale('msg')};; esac",
        f'case x in x) {GIT} commit -m "msg";; esac',
    ),
    (
        "case_script_echo",
        f"case x in x) sh -c {_locale('echo hi')};; esac",
        'case x in x) sh -c "echo hi";; esac',
    ),
    (
        # A removal the guard allows, with the quoting in its operand.
        "case_rm_operand",
        f"case x in x) rm -f a/b/c/d/{_locale('x')};; esac",
        'case x in x) rm -f a/b/c/d/"x";; esac',
    ),
]


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "guard",
    [_PUSH_GUARD, _DISCARD_GUARD, _COMMIT_GUARD, _DESTRUCTIVE_GUARD],
    ids=lambda p: p.stem,
)
@pytest.mark.parametrize(
    ("command", "plain"), [pytest.param(c, p, id=i) for i, c, p in _EMBEDDED_CONTROLS]
)
def test_locale_quoting_in_a_rebuilt_body_argument_changes_nothing(
    command, plain, guard, mode, sandbox
):
    assert sandbox.guard(command, mode, guard)[0] == sandbox.guard(plain, mode, guard)[0]
    if "sh -c" not in command:
        assert sp.analyze_checked(command)[1] is None, "ordinary text was reported"


# ── what must stay as it is ───────────────────────────────────────────────────

# Locale quoting OUTSIDE the words that choose the program, and text that only
# looks like it. Each is (id, command, the same command without the quoting form).
_CONTROLS = [
    ("message_argument", f"{GIT} commit -m {_locale('a note')}", f'{GIT} commit -m "a note"'),
    ("option_value", f"{GIT} log --grep {_locale('fix')} -1", f'{GIT} log --grep "fix" -1'),
    ("operand", f"{GIT} status {_locale('x')}", f'{GIT} status "x"'),
    ("no_gated_program", f"echo {_locale('hello')}", 'echo "hello"'),
    ("wrapper_value", f"timeout {_locale('5')} {GIT} status", f'timeout "5" {GIT} status'),
    ("assignment_value", f"X={_locale('a b')} ls", 'X="a b" ls'),
    ("directory_part_only", f"{_locale('/usr/bin/')}{GIT} status", f'"/usr/bin/"{GIT} status'),
    # The second reading still names its program with an expansion: the documented
    # residual for a program held in a variable, which this rule does not reopen.
    (
        "program_still_an_expansion",
        f"{_locale('$G')} {PUSH} origin main",
        f'"$G" {PUSH} origin main',
    ),
    ("printf_format", f"printf {_locale('%s\\n')} x", 'printf "%s\\n" x'),
    ("not_a_shell", f"python3 -c {_locale('print(1)')}", 'python3 -c "print(1)"'),
    # A shell, but the locale quoting is not what hands it its script.
    ("inside_the_script", f"sh -c 'echo {_locale('hi')}'", "sh -c 'echo \"hi\"'"),
    ("after_the_script", f"sh -c 'echo hi' {_locale('zero')}", "sh -c 'echo hi' \"zero\""),
]
# Text that holds the two characters and is not locale quoting at all.
_LOOKALIKES = [
    ("dollar_ends_a_quoted_pattern", 'grep "foo$" x'),
    ("dollar_inside_double_quotes", 'echo "a$"b'),
    ("single_quoted", "printf %s '$\"x\"'"),
    ("escaped_dollar", 'echo \\$"x"'),
    # In PROGRAM position, where a scan that ignored quoting would invent a program.
    ("single_quoted_program_word", f"'{_locale(GIT)}' {PUSH} origin main"),
    ("dollar_closes_a_quoted_prefix", f'"a$"{GIT} {PUSH} origin main'),
]


@pytest.mark.parametrize(("command", "plain"), [pytest.param(c, p, id=i) for i, c, p in _CONTROLS])
def test_locale_quoting_elsewhere_changes_nothing(command, plain, sandbox):
    """Differential: the same verdict as the command written with plain quotes."""
    _segments, blind = sp.analyze_checked(command)
    assert blind is None, f"ordinary text was reported: {blind}"
    for mode in _MODES:
        assert _guard(command, mode)[0] == _guard(plain, mode)[0]


@pytest.mark.parametrize("command", [pytest.param(c, id=i) for i, c in _LOOKALIKES])
def test_text_that_only_looks_like_locale_quoting_is_not_read_as_it(command):
    assert '$"' in command, "control: the cheap test that starts the second reading passes"
    assert sp._locale_openers(command) == []
    assert sp.analyze_checked(command)[1] is None


def test_a_special_parameter_before_a_quoted_word_is_not_locale_quoting(sandbox):
    """Two dollar signs are the shell's pid parameter, and the quotes after them
    are ordinary. The program is then a name the shell builds, which stays the
    documented residual: recorded on the segment, not reported."""
    command = f"$${chr(34)}{GIT}{chr(34)} {PUSH} origin main"
    assert sandbox.ran(command) == [], "oracle: bash runs <pid>git, which is not git"
    segments, blind = sp.analyze_checked(command)
    assert blind is None, f"read as locale quoting: {blind}"
    assert any(s.verb_unresolved for s in segments)
    assert not any(s.locale_program for s in segments)
    for mode in _MODES:
        assert _guard(command, mode)[0] == _guard(f"$${GIT} {PUSH} origin main", mode)[0]


# ── the parser ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "program"),
    [pytest.param(c, p, id=i) for i, c, p in _PROGRAM_SPELLINGS]
    + [
        pytest.param(_command("git", "program", "locale"), "git", id="whole_word_git"),
        pytest.param(_command("gh", "program", "locale"), "gh", id="whole_word_gh"),
    ],
)
def test_the_parser_reports_and_does_not_resolve(command, program):
    segments, blind = sp.analyze_checked(command)
    assert blind is not None, (
        "the parse named a program bash never runs and reported nothing, so a guard "
        "cannot tell this from a command with no gated operation"
    )
    assert blind is sp._BLIND_LOCALE_PROGRAM, f"reported as {blind!r}"
    assert not blind.bounds_induced and segments, "the segments are complete and returned"
    assert program in {s.locale_program for s in segments}
    # Reported, never decoded: no segment claims the program as its executable, so
    # no ordinary gate hands a policy verdict to a word the shell may translate.
    wanted = _wanted(program)
    assert not any(tuple(s.argv[: len(wanted)]) == wanted for s in segments)


def test_the_new_cause_has_its_own_remedy():
    spot = sp._BLIND_LOCALE_PROGRAM
    assert spot in sp._ALL_BLIND_SPOTS
    assert spot.bounds_induced is False
    assert spot.cause != sp._BLIND_UNRESOLVED_VERB.cause
    assert "subcommand" not in spot.hint, "that rewrite does not apply to a program word"
    assert "plain" in spot.hint


def test_an_existing_cause_keeps_its_wording():
    """Reported last: a command that already had a blind spot keeps the cause and
    hint it had, so this rule only ever adds coverage."""
    command = f"{GIT} $V x; {_locale(GIT)} status"
    assert sp.analyze_checked(command)[1] is sp._BLIND_UNRESOLVED_VERB


def test_an_unproven_boundary_still_withholds_the_parse():
    """Precedence where it can be wrong: the structural cause is bounds-type, and
    this one must not replace it with a weaker, segment-returning report."""
    command = f"{_locale(GIT)} status \\$(x)"
    alone = sp.analyze_checked(f"{GIT} status \\$(x)")[1]
    assert alone is not None and alone.bounds_induced, "control: the boundary is unproven"
    segments, blind = sp.analyze_checked(command)
    assert blind is not None and blind.bounds_induced and segments == []


def test_a_withheld_parse_still_names_the_program():
    continued = f"{_locale(GIT)} status \\\n -s"
    assert sp.analyze_checked(continued)[1] is sp._BLIND_CONTINUATION
    assert GIT in sp.unresolved_verb_programs(continued)


# ── the opener scan, against bash ─────────────────────────────────────────────

# (word, the index of every dollar sign that opens locale quoting in it)
_OPENERS = [
    ('$"g"it', [0]),
    ('g$"it"', [1]),
    ('$""x', [0]),
    ('x$"y"z', [1]),
    ('$"a"$"b"', [0, 4]),
    ("$'a'$\"b\"", [4]),
    ('$"don\'t"', [0]),
    ('"a$"b', []),  # inside double quotes the dollar sign is literal
    ("'$\"x\"'", []),
    ('\\$"x"', []),  # escaped
    ('\\\\$"x"', [2]),  # an escaped backslash, then locale quoting
    ('\\$$"x"', [2]),  # an escaped dollar sign, then locale quoting
    ('$$"x"', []),  # the pid parameter, then plain quotes
    ('$$$"x"', [2]),  # the pid parameter, then locale quoting
    ('$#$"x"', [2]),
    ('$?$$"x"', []),
]


# Renders each argument as bash source, one per line.
_RENDER = (
    "for w in \"$@\"; do eval \"printf '[%s]' $w\" 2>/dev/null || printf '<rejected>'; "
    "printf '\\n'; done"
)


@pytest.mark.parametrize(
    ("word", "openers"), _OPENERS, ids=[f"word{i}" for i in range(len(_OPENERS))]
)
def test_the_opener_scan_matches_bash(word, openers, sandbox):
    """Removing a dollar sign leaves bash's word unchanged exactly when the scan
    calls it a locale-quote opener. Every rendering comes from ONE bash process, so
    the pid parameter reads the same in each; a rendering bash rejects (the dollar
    sign was holding the quoting together) counts as changed."""
    assert sp._locale_openers(word) == openers
    candidates = [k for k in range(len(word) - 1) if word[k : k + 2] == '$"']
    variants = [word[:k] + word[k + 1 :] for k in candidates]
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", _RENDER, "render", word, *variants],
        env={"PATH": "/usr/bin:/bin", "HOME": str(sandbox.work), "LC_ALL": "C"},
        cwd=sandbox.work,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    as_written, *as_plain = done.stdout.splitlines()
    assert as_written != "<rejected>" and len(as_plain) == len(candidates), done.stderr
    for at, rendering in zip(candidates, as_plain, strict=True):
        assert (rendering == as_written) == (at in openers), (
            f"dollar sign at {at}: bash gives {as_written} with it and {rendering} without"
        )


@pytest.mark.parametrize(
    "text",
    ["a $'b' c", "$'a\\x41'", "\"$'x'\" $'y'", "$'unterminated", "echo $\"don't\" $'z'"],
    ids=["plain_span", "escape", "inside_dq", "unterminated", "after_locale"],
)
def test_collecting_openers_leaves_the_ansi_c_spans_alone(text):
    """The opener scan rides the ANSI-C scan's quote state; its other callers read
    the spans, which must not move."""
    assert sp._ansi_c_spans(text, []) == sp._ansi_c_spans(text)


# ── cost ──────────────────────────────────────────────────────────────────────


def _tokenizations(command: str, monkeypatch) -> int:
    calls: list[str] = []
    real = shlex.split

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(sp.shlex, "split", counting)
    sp.analyze_checked(command)
    monkeypatch.setattr(sp.shlex, "split", real)
    return len(calls)


def test_the_second_reading_is_one_tokenization_per_segment(monkeypatch):
    """A guard killed by its clock permits, so the extra reading must not grow
    with the number of openers: one more tokenization of the segment, however many
    it holds."""
    one = _tokenizations("echo " + _locale("w"), monkeypatch)
    many = _tokenizations("echo " + " ".join(_locale("w") for _ in range(400)), monkeypatch)
    assert many == one


def test_the_script_reading_adds_no_tokenization_per_opener(monkeypatch):
    """The script candidate comes from the same second reading: a shell's argv with
    many openers costs what one costs, and the candidate is never parsed."""
    one = _tokenizations("sh -c " + _locale("w"), monkeypatch)
    many = _tokenizations(
        "sh -c " + _locale("w") + " " + " ".join(_locale("w") for _ in range(400)), monkeypatch
    )
    assert many == one


def test_reading_a_rebuilt_body_is_bounded_by_the_parse_depth(monkeypatch):
    """A rebuilt body is read level by level, and the levels stop where the parse's
    own descent stops: a longer chain of constructs adds no tokenization, and many
    openers in one body cost what one costs."""
    short = _tokenizations("coproc " * 20 + f"{_locale(GIT)} {PUSH}", monkeypatch)
    long = _tokenizations("coproc " * 400 + f"{_locale(GIT)} {PUSH}", monkeypatch)
    assert long == short
    one = _tokenizations(f"case x in x) echo {_locale('w')};; esac", monkeypatch)
    many = _tokenizations(
        "case x in x) echo " + " ".join(_locale("w") for _ in range(400)) + ";; esac",
        monkeypatch,
    )
    assert many == one


def test_a_command_without_the_quoting_form_takes_no_second_reading(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("a second reading ran for text holding no locale quoting")

    monkeypatch.setattr(sp, "_locale_reading", unexpected)
    segments, blind = sp.analyze_checked(f"{GIT} {PUSH} origin main && echo $HOME 'a b'")
    assert blind is None and [s.exe for s in segments] == [GIT, "echo"]
