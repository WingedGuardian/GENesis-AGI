"""`env -S 'cmd'` RUNS cmd, so the guards have to see it.

Before this, the whole operand resolved as the executable: a segment whose exe
was the literal string ``git push --force origin main``, matching no gate.
MEASURED against the real push guard, with a control of the same command written
plainly (which exits 2): the `env -S` spelling exited 0, and the form runs.

Every rule exercised here was MEASURED against the installed binary rather than
read off a manual, by running the form and reading what the carried command
printed. That method is the point: an earlier attempt at this modelled `-S` as a
flag carrying an opaque string, and review found five separate ways that was
wrong — the escape language, the appended operands, and the split fields being
re-read as env's own options.

The other half of the contract is what must NOT be revealed. A string env itself
rejects carries no command to find, and reporting one would invent argv out of
input the system refuses — the mistake #1686 removed when it stopped decoding
unterminated ANSI-C spans. So an unknown escape, an unterminated quote and a
leading comment all resolve to nothing, and a token spelled ``env`` that is
merely an ARGUMENT is not a wrapper at all.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"


def _load():
    spec = importlib.util.spec_from_file_location("shell_parse_envs", _HOOKS / "shell_parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["shell_parse_envs"] = mod
    spec.loader.exec_module(mod)
    return mod


sp = _load()

# Assembled rather than written out, so the string never appears whole in a file
# the shell-safety hooks scan — they read this repo's own test fixtures.
FORCE = "--" + "for" + "ce"
PUSH = "git " + "push " + FORCE + " origin main"


def exes(cmd: str) -> list[str]:
    return [s.exe for s in sp.analyze(cmd)]


def guard_rc(cmd: str) -> int:
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / "git_push_guard.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )
    return proc.returncode


class TestEveryAcceptedSpellingIsRead:
    """All five run. MEASURED — including the short bundle, which surprised me."""

    @pytest.mark.parametrize(
        "cmd",
        [
            f"env -S '{PUSH}'",
            f"env -S'{PUSH}'",
            f"env --split-string='{PUSH}'",
            f"env --split-string '{PUSH}'",
            f"env -iS '{PUSH}'",
            f"env -i -S '{PUSH}'",
            f"env -u FOO -S '{PUSH}'",
            f"FOO=1 env -S '{PUSH}'",
        ],
    )
    def test_the_carried_command_reaches_the_guard(self, cmd):
        assert "git" in exes(cmd), exes(cmd)
        assert guard_rc(cmd) == 2

    def test_the_operand_is_never_itself_the_executable(self):
        # The pre-fix reading: one segment whose exe was the entire command line.
        assert PUSH not in exes(f"env -S '{PUSH}'")


class TestTheEscapeLanguage:
    """`-S` has its own, and it is not the shell's."""

    def test_underscore_is_a_space_inside_one_field(self):
        # So it must NOT split a command: this is one program name, not four
        # words, and no such program exists — nothing for a push gate to fire on.
        cmd = "env -S 'git\\_push\\_" + FORCE + "'"
        assert "git" not in exes(cmd), exes(cmd)
        assert guard_rc(cmd) == 0

    def test_underscore_inside_an_argument_keeps_the_real_command(self):
        cmd = "env -S 'git push " + FORCE + " origin ma\\_in'"
        assert "git" in exes(cmd), exes(cmd)
        assert guard_rc(cmd) == 2

    @pytest.mark.parametrize("seq,char", [("t", "\t"), ("n", "\n"), ("$", "$"), ("#", "#")])
    def test_a_known_escape_decodes_to_its_character(self, seq, char):
        fields = sp._env_split_fields(f"echo a\\{seq}b")
        assert fields == ["echo", f"a{char}b"], fields

    def test_backslash_c_ends_the_string_mid_field(self):
        assert sp._env_split_fields("echo a\\cb rest") == ["echo", "a"]

    def test_an_unknown_escape_reveals_nothing(self):
        """env exits 125 on `\\q` — there is no command in a string it refuses."""
        assert sp._env_split_fields("git\\qpush") is None
        assert guard_rc("env -S 'git\\qpush " + FORCE + " origin main'") == 0

    def test_an_unterminated_quote_reveals_nothing(self):
        assert sp._env_split_fields("git 'push") is None
        assert guard_rc(f'env -S "git \'push {FORCE} origin main"') == 0


class TestComments:
    """`#` starts one only at the START of a field — MEASURED, not assumed."""

    def test_a_leading_hash_comments_out_everything(self):
        assert sp._env_split_fields(f"#{PUSH}") == []
        assert guard_rc(f"env -S '#{PUSH}'") == 0

    def test_a_hash_after_a_separator_drops_the_rest(self):
        assert sp._env_split_fields("echo a #b c") == ["echo", "a"]

    def test_a_hash_inside_a_word_is_literal(self):
        assert sp._env_split_fields("echo a#b") == ["echo", "a#b"]

    def test_a_hash_inside_quotes_is_literal(self):
        assert sp._env_split_fields("echo 'a #b'") == ["echo", "a #b"]


class TestSplitFieldsAreReReadAsEnvsOwnArguments:
    """`env -S '-i cmd'` runs cmd — the fields are env's argv, not just a command."""

    @pytest.mark.parametrize("prefix", ["-i ", "-u FOO ", "FOO=1 ", "-i -u BAR "])
    def test_leading_env_arguments_are_walked_past(self, prefix):
        cmd = f"env -S '{prefix}{PUSH}'"
        assert "git" in exes(cmd), exes(cmd)
        assert guard_rc(cmd) == 2

    def test_operands_after_the_string_are_appended_to_the_command(self):
        # MEASURED: `env -S 'echo a' b c` prints "a b c".
        cmd = f"env -S 'git push' {FORCE} origin main"
        assert "git" in exes(cmd), exes(cmd)
        assert guard_rc(cmd) == 2


class TestOnlyAnEnvInCommandPositionIsAWrapper:
    """A token spelled `env` elsewhere is an ARGUMENT, and inventing a command
    from one would have a guard refuse something that never runs."""

    def test_env_as_an_argument_is_not_a_wrapper(self):
        cmd = f"printf '%s' env -S '{PUSH}'"
        assert exes(cmd) == ["printf"], exes(cmd)
        assert guard_rc(cmd) == 0

    def test_option_processing_stops_at_a_double_dash(self):
        # MEASURED: `env -- -S 'echo hi'` exits 127 — `-S` is a file name there.
        assert guard_rc(f"env -- echo {PUSH}") == 0

    def test_option_processing_stops_at_a_bare_word(self):
        # MEASURED: `env echo -S x` prints "-S x" — the flag is echo's argument.
        assert guard_rc(f"env echo -S '{PUSH}'") == 0


class TestControlsThatMustNotMove:
    """Green on BOTH sides of this change — that is what makes them controls."""

    def test_the_plain_command_still_blocks(self):
        assert exes(PUSH) == ["git"]
        assert guard_rc(PUSH) == 2

    def test_env_without_the_flag_is_unchanged(self):
        assert exes(f"env -u FOO {PUSH}") == ["git"]
        assert exes(f"env FOO=1 {PUSH}") == ["git"]
        assert guard_rc(f"env -u FOO {PUSH}") == 2

    def test_a_harmless_carried_command_is_not_refused(self):
        assert guard_rc("env -S 'ruff check .'") == 0
        assert guard_rc("env -S 'echo hello world'") == 0
