"""A wrapper's option arity must match the tool's, and a wrong entry fails OPEN.

``_WRAPPER_SPEC`` tells the resolver which of a wrapper's own options consume the
NEXT token, so the walk can reach the command being wrapped. It is hand-written,
and it has two failure directions that are not symmetric:

* An option MISSING from it: its value is read as the command. The resolved exe
  is then the value, which usually matches no gate — bad, but visible.
* An option WRONGLY listed: the walk eats the command WORD. The exe becomes the
  command's first argument, and a gate keyed on the exe never fires.

The second is the fail-open, and it is the one that had gone unnoticed here.
MEASURED 2026-09-13 against the real binaries and the real guards, each form
executed first so that a mis-parse of an unrunnable command could not be mistaken
for a bypass:

    xargs -i <push>     RUNS, resolved past `git`, push guard exit 0
    xargs -e <push>     RUNS, resolved past `git`, push guard exit 0

against a control of the same push written plainly, which exits 2. `xargs`
documents ``--eof[=END]`` and ``--replace[=R]`` — OPTIONAL values, so a bare
``-e``/``-i`` consumes nothing. This is the failure ``--isolated`` already taught
the uv table, which is why the last class here is a LOCK rather than a longer
list: the table is re-derived from each tool's own ``--help``, and an option
listed without a documented REQUIRED value fails the suite.

Two things are out of scope on purpose, and saying which is which matters more
than the fact that both are excluded:

* An option simply ABSENT from a table. That is the visible direction, and the
  case that prompted the search — a wrapper given an option it does not
  recognise — turned out not to be a bypass at all: the wrapper exits 125 and
  never runs the command, so nothing is permitted. Same reason #1686 stopped
  decoding unterminated ANSI-C spans rather than parsing them.
* ``env --split-string``, which is a REAL fail-open and is NOT fixed here. It
  carries a whole command line in one token, so it needs the nested walk rather
  than a table entry — and its own escape language, its appended arguments and
  its re-reading of the split fields as env's own options make it a grammar to
  model against the binary. It has its own PR. Adding it to the table without
  that walk would HIDE the command instead of mis-reading it, which is worse.
"""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"


def _load():
    spec = importlib.util.spec_from_file_location("shell_parse_arity", _HOOKS / "shell_parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["shell_parse_arity"] = mod
    spec.loader.exec_module(mod)
    return mod


sp = _load()

PUSH = "git " + "push --" + "for" + "ce origin main"


def exes(cmd: str) -> list[str]:
    return [s.exe for s in sp.analyze(cmd)]


def guard_rc(cmd: str, guard: str = "git_push_guard.py") -> int:
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / guard)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )
    return proc.returncode


class TestControlsThatMustNotMove:
    """Every one of these resolved and blocked correctly BEFORE the fix too."""

    def test_the_plain_form_blocks(self):
        assert exes(PUSH) == ["git"]
        assert guard_rc(PUSH) == 2

    @pytest.mark.parametrize(
        "cmd",
        [
            "env -u FOO " + PUSH,
            "timeout -k 1 5 " + PUSH,
            "nice -n 5 " + PUSH,
            "stdbuf -o L " + PUSH,
            "echo x | xargs -I R " + PUSH,
            "echo x | xargs -n 1 " + PUSH,
            "echo x | xargs -E END " + PUSH,
        ],
    )
    def test_correctly_tabled_options_still_reveal_the_command(self, cmd):
        assert "git" in exes(cmd)
        assert guard_rc(cmd) == 2


class TestWronglyListedOptionsNoLongerEatTheCommand:
    """`-e`/`-i` take OPTIONAL values, so a bare one consumes nothing."""

    @pytest.mark.parametrize("flag", ["-i", "-e", "--replace", "--eof"])
    def test_an_optional_value_flag_does_not_consume_the_command(self, flag):
        cmd = f"echo x | xargs {flag} {PUSH}"
        assert "git" in exes(cmd), f"{flag} ate the command word"
        assert guard_rc(cmd) == 2

    def test_the_required_value_siblings_are_unaffected(self):
        # -E and -I take REQUIRED separate values and must still consume them.
        assert exes("echo x | xargs -E END " + PUSH) == ["echo", "git"]
        assert exes("echo x | xargs -I R " + PUSH) == ["echo", "git"]


# ── the lock ────────────────────────────────────────────────────────────

# EVERY option spelling in a line's DECLARATION region, each with whatever
# immediately follows it. Reading only a leading `-x, --long` pair was too narrow
# for the layouts these tools actually print: `xvfb-run` writes
# `-n NUM    --server-num=NUM`, putting the placeholder BETWEEN the two
# spellings, so the long form was never seen at all. The lookbehind keeps a
# hyphen inside a description word ("non-blank") from reading as an option, and
# the digit is there because `xargs` documents `-0, --null` — a numeric spelling
# an earlier version skipped, which left it unparsed and therefore unchecked.
_OPTION_AT = re.compile(r"(?:(?<=^)|(?<=\s))--?[A-Za-z0-9][-A-Za-z0-9]*")
# Where the DESCRIPTION starts: a column gap followed by a word. Everything after
# it is prose, and prose REFERS to options it does not declare — `xargs` writes
# `-I R    same as --replace=R`, which read as a declaration reported `--replace`
# as value-taking. That is not a cosmetic slip: it made the lock pass the exact
# table entry this change exists to remove. A following `-` is NOT a description,
# because that is the second spelling in `-e FILE   --error-file=FILE`.
_DESCRIPTION_AT = re.compile(r"\s\s+[A-Za-z]")
# `-e, --eof[=END]` — the two spellings are separated by nothing but a comma, so
# they SHARE the one tail that follows the pair. Distinguished from
# `-n NUM  --server-num=NUM`, where the gap carries the short form's own value.
_PAIR_GAP = re.compile(r"^,?\s*$")


# A documented REQUIRED value, in the three shapes these tools actually print:
# `=FILE`, ` <class>`, and a bare all-caps placeholder. The placeholder may be a
# SINGLE letter — `xargs` documents `-I R` and `-E END` — so the length floor
# that felt safer here was wrong in the noisy direction: it read `-I R` as
# zero-argument and failed the lock on a CORRECT entry. The trailing `\b` is what
# keeps an ordinary capitalised description word out ("Reopen stdin as …" does
# not match, because `R` is followed by a lowercase letter and no boundary).
_REQUIRED_TAIL = re.compile(r"^(=\S| <[^>]+>| [A-Za-z][-A-Za-z0-9]*\b)")


def _documented_arity(help_text: str) -> dict[str, str]:
    """Each option -> ``"required"`` | ``"optional"`` | ``"zero"``, per the tool's own help.

    Only ``required`` is safe to list in ``_WRAPPER_SPEC``. Both of the other two
    make the resolver consume a token the tool does not, which is the command
    word — so the lock has to tell all three apart, not merely spot the optional
    ones. Reporting only ``optional`` was this check's own blind spot: a boolean
    added by mistake (``xargs -t``) passed it while eating the wrapped command.

    Every spelling on a line is read with its OWN tail rather than one arity
    being inferred for the pair, because the layouts differ: coreutils writes
    ``-e, --eof[=END]`` (one tail, shared) while ``xvfb-run`` writes
    ``-n NUM    --server-num=NUM`` (two tails, and the long form does not follow
    the short one at all). Reading only the leading pair missed the second shape
    entirely — and reading only the LONG form was an earlier blind spot of this
    same function, when the two entries that mattered were short ones.

    A name seen more than once keeps the STRICTEST reading. That is what makes a
    WRAPPED DESCRIPTION line harmless: ``xvfb-run`` continues the text for ``-a``
    onto a line reading only ``--server-num``, which in isolation looks like a
    boolean and would have failed the lock on a correct entry.

    An option the help does not mention is absent from the result rather than
    guessed at — see the caller, which skips what it cannot measure.
    """
    rank = {"zero": 0, "optional": 1, "required": 2}
    arity: dict[str, str] = {}
    for raw_line in help_text.splitlines():
        if not raw_line.strip().startswith("-"):
            continue
        cut = _DESCRIPTION_AT.search(raw_line)
        line = raw_line[: cut.start()] if cut else raw_line
        hits = [(m.start(), m.end(), m.group(0)) for m in _OPTION_AT.finditer(line)]
        kinds: list[str] = []
        for idx, (_, end, _name) in enumerate(hits):
            stop = hits[idx + 1][0] if idx + 1 < len(hits) else len(line)
            tail = line[end:stop]
            cut = tail.find("  ")  # a description begins at the column gap
            if cut != -1:
                tail = tail[:cut]
            if tail.startswith("["):
                kinds.append("optional")
            elif _REQUIRED_TAIL.match(tail):
                kinds.append("required")
            else:
                kinds.append("zero")
        # A spelling whose gap to the next one is only a comma shares its arity.
        for idx in range(len(hits) - 2, -1, -1):
            gap = line[hits[idx][1] : hits[idx + 1][0]]
            if _PAIR_GAP.match(gap):
                kinds[idx] = kinds[idx + 1]
        for (_, _, name), kind in zip(hits, kinds, strict=True):
            if name not in arity or rank[kind] > rank[arity[name]]:
                arity[name] = kind
    return arity


#: Wrappers that are shell BUILTINS, not files. `shutil.which` returns None for
#: every one of them, so asking the filesystem skipped them permanently and the
#: lock never checked a single entry — while `command`, `exec` and `time` are as
#: able to eat a wrapped command as any binary (`exec -a name cmd`). Their
#: authority is bash's own `help`, and for `time` that distinction is load
#: bearing: `/usr/bin/time` is a DIFFERENT program from the shell keyword.
_SHELL_BUILTINS = frozenset({"command", "exec"})
#: `time` is deliberately in NEITHER path, and the reason is that it is two
#: different tools wearing one name. Bash's `time` is a reserved word whose only
#: option is `-p`; `/usr/bin/time` is a separate program with `-o`/`-f`, which is
#: what `_WRAPPER_SPEC["time"]` actually describes, and which a segment reaches
#: as `/usr/bin/time …` (basename `time`). Checking the table against bash's
#: help would fail four correct entries; checking it against the external
#: program's help would validate the keyword against a binary that need not be
#: installed. One authority cannot settle it, so the lock says so rather than
#: picking the one that happens to pass.
_UNVERIFIABLE = frozenset({"time"})


def _help_text(tool: str) -> str | None:
    if tool in _SHELL_BUILTINS:
        proc = subprocess.run(
            ["bash", "-c", f"help {tool}"], capture_output=True, text=True, timeout=10
        )
        text = proc.stdout or ""
        return text if len(text) > 40 else None
    if not shutil.which(tool):
        return None
    for args in ((tool, "--help"), (tool, "-h")):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        text = (proc.stdout or "") + (proc.stderr or "")
        if len(text) > 80:
            return text
    return None


class TestTableAgreesWithTheToolsThemselves:
    """Re-derive arity from each installed tool and fail on the FAIL-OPEN direction.

    Only the dangerous direction is enforced. A missing entry is left alone on
    purpose: it is the visible direction, and several omissions are correct
    (sudo documents `--preserve-env[=list]`, which must NOT be added).

    Tools absent from this machine are skipped rather than assumed. A table
    written from memory about an uninstalled tool is the thing this lock exists
    to prevent, so it declines to guess in exactly that case.
    """

    @pytest.mark.parametrize("tool", sorted(sp._WRAPPER_SPEC))
    def test_every_listed_option_documents_a_required_value(self, tool):
        if tool in _UNVERIFIABLE:
            pytest.skip(f"{tool} has no single authority to check against — see _UNVERIFIABLE")
        help_text = _help_text(tool)
        if help_text is None:
            pytest.skip(f"{tool} is not installed here — arity cannot be measured")
        arity = _documented_arity(help_text)
        # An option this parser never SAW is reported as unverified, not waved
        # through. Defaulting it to `required` was a hole in the lock itself:
        # `xargs` documents `-0, --null`, a numeric spelling the pattern skipped,
        # so adding that boolean to the table would have passed silently — the
        # exact regression this test exists to prevent, hidden by the test.
        wrong = sorted(
            (opt, arity.get(opt, "NOT FOUND in --help"))
            for opt in sp._WRAPPER_SPEC[tool][0]
            if arity.get(opt) != "required"
        )
        assert not wrong, (
            f"{tool}: {wrong} listed as value-consuming, but the tool documents no "
            f"REQUIRED value — the resolver will consume a token the tool does not, "
            f"and that token is the wrapped command"
        )

    def test_the_lock_can_see_a_short_form(self):
        """Guard the guard: the defect this was written for was a SHORT option.

        A version of this check that only read long forms passed over `-e`/`-i`
        entirely, so it would have reported the broken table as clean.
        """
        arity = _documented_arity("  -e, --eof[=END]  set logical EOF")
        assert arity == {"-e": "optional", "--eof": "optional"}

    def test_the_lock_separates_all_three_arities(self):
        """Guard the guard, second blind spot: a BOOLEAN listed by mistake.

        Reporting only the optional ones let `xargs -t` — which takes no value at
        all — pass this check while the resolver ate the command after it. Both
        non-required readings have to fail, so all three are told apart.
        """
        assert _documented_arity("  -s, --signal=SIGNAL  specify") == {
            "-s": "required",
            "--signal": "required",
        }
        assert _documented_arity("  -t, --verbose  print commands") == {
            "-t": "zero",
            "--verbose": "zero",
        }
        assert _documented_arity("  -E END   set logical EOF string") == {"-E": "required"}
        assert _documented_arity("  -c, --class <class>  scheduling class") == {
            "-c": "required",
            "--class": "required",
        }

    def test_a_boolean_added_by_mistake_fails_the_lock(self):
        """The lock must FAIL on the shape it exists to catch, not merely pass today.

        `xargs -t` executes the command after it; listing it would make the
        resolver consume that command as a value. Asserted against the real
        `xargs` help so this cannot pass on a hand-made fixture the tool would
        never emit.
        """
        help_text = _help_text("xargs")
        if help_text is None:
            pytest.skip("xargs is not installed here")
        arity = _documented_arity(help_text)
        assert arity.get("-t") == "zero", arity.get("-t")
        assert arity.get("-i") == "optional", arity.get("-i")
        assert arity.get("-I") == "required", arity.get("-I")

    def test_the_entry_this_change_removes_would_fail_the_lock(self):
        """The sharpest guard-the-guard: re-add the exact bad entry, expect a fail.

        `--replace` is the entry this change deletes. An earlier lock reported it
        as `required` — because `xargs` writes `-I R   same as --replace=R` in a
        DESCRIPTION, and reading prose as a declaration let the strictest-reading
        rule promote it. The lock passed the very table entry it was built to
        catch. Everything else here can be green while that is true, so this
        assertion is the one that matters.
        """
        help_text = _help_text("xargs")
        if help_text is None:
            pytest.skip("xargs is not installed here")
        arity = _documented_arity(help_text)
        assert arity.get("--replace") == "optional", (
            f"--replace read as {arity.get('--replace')!r}: a description "
            f"reference is being taken for a declaration again"
        )
        assert arity.get("--eof") == "optional", arity.get("--eof")

    def test_a_spelling_the_parser_cannot_read_is_reported_not_assumed(self):
        """`xargs -0, --null` is numeric, and an unread option must not pass.

        Defaulting an unparsed option to `required` meant the lock waved through
        precisely what it could not see — the failure mode is silent, so the
        numeric spelling is parsed AND the caller treats an absent reading as a
        failure rather than a pass.
        """
        help_text = _help_text("xargs")
        if help_text is None:
            pytest.skip("xargs is not installed here")
        arity = _documented_arity(help_text)
        assert arity.get("-0") == "zero", arity.get("-0")
        assert arity.get("--null") == "zero", arity.get("--null")

    def test_a_shell_builtin_is_checked_against_bash_not_the_filesystem(self):
        """`exec`/`command` are builtins; `which` returns None for both.

        Asking the filesystem skipped them permanently, so the lock never read a
        single entry for a wrapper that can eat a command (`exec -a name cmd`).
        """
        help_text = _help_text("exec")
        assert help_text is not None, "bash help must answer for a builtin"
        assert _documented_arity(help_text).get("-a") == "required"
