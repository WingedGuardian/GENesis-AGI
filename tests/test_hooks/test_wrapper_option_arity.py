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
#: One spelling, two conflicting declarations in the same help text. NOT a
#: fourth arity — a statement that this source cannot settle the question, so
#: the lock abstains instead of grading against a coin flip. It is deliberately
#: NOT equal to any real arity, so a caller that forgets to handle it fails the
#: entry rather than silently accepting it.
_AMBIGUOUS = "ambiguous"
#: Spellings this repo has REVIEWED and accepted as unsettleable from the tool's
#: own help. An entry is a DECLARED known gap: the option is listed in
#: `_WRAPPER_SPEC` and the lock does not grade it. Anything ambiguous that is NOT
#: named here FAILS, so a new unsettleable spelling cannot enter the table
#: silently.
#:
#: Polarity matters, and the previous shape had it backwards: it PRINTED the
#: abstention from a PASSING test and carried on. MEASURED on this branch with
#: CI's own flags (`pytest -rfE --junit-xml`): zero occurrences of that message
#: in the log AND zero in the JUnit report, because pytest discards a passing
#: test's captured output and `junit_logging` defaults to `no`. Adding a second
#: ambiguous-but-invalid entry such as `sudo --preserve-env` therefore produced
#: a completely green, silent build — a warning routed where its reader never
#: looks (Codex P2, PR #1986 round 5).
#:
#: Anchored to an exact (tool, option) pair, never a prefix, and never shared
#: across tools. An entry that STOPS being ambiguous — the tool's help changed —
#: goes inert rather than stale: that option falls back to ordinary grading,
#: which passes if the help now documents a required value and fails loudly if
#: it does not. So a dead entry can hide nothing beyond the one ambiguity it
#: names, and no staleness assertion is needed to keep it honest.
#:
#: `sudo -h` is the only member. The help declares `-h, --help` (zero) AND
#: `-h, --host=host` (required). Strictest-wins reports `required`, which would
#: wave through `--preserve-env` and let the resolver eat the wrapped command;
#: weakest-wins fails `-h`, which IS correctly value-consuming. The
#: disambiguating fact is not in the help text at all, and `sudo` is a binary,
#: so `_parser_arity` cannot read its getopt spec either.
_KNOWN_AMBIGUOUS: dict[str, frozenset[str]] = {"sudo": frozenset({"-h"})}


def _unexpected_ambiguities(tool: str, arity: dict[str, str], listed) -> list[str]:
    """Listed options this help declares two ways that are NOT a reviewed exception.

    Extracted from the caller so the allowlist's polarity is directly testable:
    the property that matters is that an UNLISTED ambiguity is returned (and so
    fails), which a test driving the whole parametrized check could only observe
    on a tool that happens to be ambiguous on this machine.
    """
    allowed = _KNOWN_AMBIGUOUS.get(tool, frozenset())
    return sorted(opt for opt in listed if arity.get(opt) == _AMBIGUOUS and opt not in allowed)


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

    A name seen more than once with CONFLICTING readings is ``_AMBIGUOUS``, and
    the caller abstains on it rather than grading against a guess. This used to
    keep the STRICTEST reading, which is wrong in a measurable direction:
    ``sudo`` documents ``-E, --preserve-env`` (bare) AND ``--preserve-env=list``,
    so strictest reports ``required`` and adding that option to the table would
    PASS the lock while the resolver ate the wrapped command (Codex P2, PR
    #1986). The obvious repair — keep the WEAKEST — is equally wrong one entry
    over: ``sudo`` also documents ``-h, --help`` and ``-h, --host=host``, and
    ``-h`` IS correctly listed as value-consuming, so weakest fails a correct
    entry. MEASURED across every table entry: flipping to weakest produced
    exactly one new failure, ``sudo -h``.

    Two structurally identical inputs needing opposite verdicts is not a rule
    to tune — the disambiguating fact is not in the help text at all. So the
    lock says so instead of guessing.

    The wrapped-description case that motivated strictest-wins is handled a
    layer up rather than here: ``xvfb-run`` continues the text for ``-a`` onto a
    line reading only ``--server-num``, which conflicts with its own definition
    line — but `_parser_arity` reads that tool's getopt spec and OVERRIDES this
    reading entirely, so the ambiguity never reaches the caller. A tool whose
    parser CAN be read is never abstained on; abstention is for prose alone.

    An option the help does not mention is absent from the result rather than
    guessed at — see the caller, which skips what it cannot measure.
    """
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
            if name not in arity:
                arity[name] = kind
            elif arity[name] != kind:
                # CONFLICTING declarations for one spelling. Neither reading can
                # be preferred, and picking one is wrong in a measurable
                # direction whichever you pick — see `_AMBIGUOUS`.
                arity[name] = _AMBIGUOUS
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


#: A shell script's `getopt` call IS its option parser; `--help` is prose ABOUT
#: that parser, and the two can disagree. MEASURED 2026-09-19 against xvfb
#: 2:21.1.12: the installed `xvfb-run` carries `-w|--wait` in its
#: `--options +ae:f:hn:lp:s:w:` spec, in its `--long …,wait:` spec and in its
#: case block, taking a REQUIRED value each time — and documents neither
#: spelling in `--help`. Checking that row against the help alone failed a
#: CORRECT entry, which this file's own docstring calls worse than no lock; and
#: the obvious repair, deleting the row, would have opened the fail-open hole
#: the table exists to close (`xvfb-run -w 5 <cmd>` resolving `5` as the exe).
#:
#: So the parser outranks the prose wherever the parser can be read. This is
#: also where `_WRAPPER_SPEC["xvfb-run"]` came from in the first place — its
#: comment says "not from `--help` prose" — so until now the lock was grading
#: that row against an authority the row had explicitly declined to use.
_GETOPT_SHORT_AT = re.compile(r"--options[=\s]+\+?-?([A-Za-z0-9:]+)")
_GETOPT_LONG_AT = re.compile(r"--long(?:options)?[=\s]+([A-Za-z0-9,:_-]+)")
#: getopt's grammar: no colon takes nothing, one is required, two is optional.
_ARITY_BY_COLONS = ("zero", "required", "optional")


def _getopt_arity(script: str) -> dict[str, str]:
    """Each option -> arity, read from a shell script's own ``getopt`` spec.

    Returns ``{}`` when the script runs no ``getopt``, so a caller falls back to
    the help text rather than reading silence as a clean bill of health.
    """
    arity: dict[str, str] = {}
    short = _GETOPT_SHORT_AT.search(script)
    if short:
        spec = short.group(1)
        i = 0
        while i < len(spec):
            name, i = spec[i], i + 1
            colons = 0
            while i < len(spec) and spec[i] == ":":
                colons, i = colons + 1, i + 1
            arity[f"-{name}"] = _ARITY_BY_COLONS[min(colons, 2)]
    long_spec = _GETOPT_LONG_AT.search(script)
    if long_spec:
        for entry in long_spec.group(1).split(","):
            name = entry.rstrip(":")
            if name:
                arity[f"--{name}"] = _ARITY_BY_COLONS[min(len(entry) - len(name), 2)]
    return arity


def _parser_arity(tool: str) -> dict[str, str]:
    """The tool's own option parser, when it is a readable script running ``getopt``.

    Empty for a builtin, for a tool that is not installed, and for a compiled
    binary — none of those has a spec to read, and guessing at one would be the
    hole this lock exists to close.
    """
    if tool in _SHELL_BUILTINS:
        return {}
    path = shutil.which(tool)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(65536)
    except OSError:
        return {}
    if not head.startswith("#!"):
        return {}
    return _getopt_arity(head)


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
        # The tool's OWN parser wins wherever it can be read, because it is what
        # actually runs; the help is prose about it and can omit an option
        # entirely (see `_parser_arity`). Empty for every binary and builtin, so
        # this narrows nothing for the tools that have no spec to read.
        arity.update(_parser_arity(tool))
        # A spelling the help declares two ways cannot be graded here — the
        # parser reading above has already resolved it wherever a parser could be
        # read, so what remains is genuinely unsettleable from prose. That makes
        # it a DECLARED exception or a failure, never a silent exclusion: see
        # `_KNOWN_AMBIGUOUS` for why printing it instead reached nobody.
        surprises = _unexpected_ambiguities(tool, arity, sp._WRAPPER_SPEC[tool][0])
        assert not surprises, (
            f"{tool}: {surprises} — the help declares each of these two ways and no "
            f"parser was readable, so the lock cannot grade them. Settle the arity "
            f"against the tool's real parser, or name it in _KNOWN_AMBIGUOUS with the "
            f"measurement that justifies it. An ungraded entry is a KNOWN GAP, never "
            f"a pass."
        )
        # An option neither source SAW is reported as unverified, not waved
        # through. Defaulting it to `required` was a hole in the lock itself:
        # `xargs` documents `-0, --null`, a numeric spelling the pattern skipped,
        # so adding that boolean to the table would have passed silently — the
        # exact regression this test exists to prevent, hidden by the test.
        wrong = sorted(
            (opt, arity.get(opt, "NOT FOUND in --help or the tool's own getopt spec"))
            for opt in sp._WRAPPER_SPEC[tool][0]
            if arity.get(opt) != "required" and arity.get(opt) != _AMBIGUOUS
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

    def test_the_getopt_reader_separates_all_three_arities(self):
        """Guard the guard: getopt's colon grammar is the whole contract.

        One colon is a required value, two is optional, none takes nothing —
        and reading a bare name as value-consuming is the fail-open direction,
        so all three are pinned rather than only the one that mattered.
        """
        arity = _getopt_arity('ARGS=$(getopt --options +ae:f:: --long auto,err:,pad:: -- "$@")')
        assert arity == {
            "-a": "zero",
            "-e": "required",
            "-f": "optional",
            "--auto": "zero",
            "--err": "required",
            "--pad": "optional",
        }

    def test_a_script_with_no_getopt_says_nothing_rather_than_passing_everything(self):
        """An empty reading must not read as agreement.

        If silence meant "fine", the reader would wave through every entry for
        every script that parses its own argv by hand — the lock would still be
        green and would be measuring nothing.
        """
        assert _getopt_arity("#!/bin/sh\nwhile [ $# -gt 0 ]; do shift; done\n") == {}
        assert _parser_arity("definitely-not-an-installed-tool-xyz") == {}

    def test_the_parser_outranks_a_help_that_omits_the_option(self):
        """The defect that sent this lock after a correct entry.

        `xvfb-run` takes `-w`/`--wait` with a REQUIRED value and documents
        neither in `--help`. Skipped where the tool is absent, because a table
        asserted about an uninstalled tool is what this lock exists to prevent.
        """
        if not shutil.which("xvfb-run"):
            pytest.skip("xvfb-run is not installed here — the disagreement cannot be measured")
        spec = _parser_arity("xvfb-run")
        assert spec.get("-w") == "required", "the getopt spec must carry the short form"
        assert spec.get("--wait") == "required", "and the long form"
        help_text = _help_text("xvfb-run")
        assert help_text is not None
        documented = _documented_arity(help_text)
        assert "-w" not in documented and "--wait" not in documented, (
            "this test pins a DISAGREEMENT between the parser and its prose; if the help "
            "has since grown the option, the disagreement is gone and so is the need for "
            "the reader to outrank it here"
        )

    def test_a_spelling_declared_two_ways_is_AMBIGUOUS_not_guessed(self):
        """Guard the guard: the sentinel must be distinct from every real arity.

        Keeping the STRICTEST reading here let `sudo --preserve-env` report
        `required`, so adding that option to the table would have PASSED the
        lock while the resolver ate the wrapped command. Keeping the WEAKEST is
        equally wrong for `sudo -h`, which IS correctly value-consuming. Neither
        rule works, so neither is used (Codex P2, PR #1986).
        """
        arity = _documented_arity(
            "  -E, --preserve-env      preserve the environment\n"
            "      --preserve-env=list preserve specific variables\n"
        )
        assert arity["--preserve-env"] == _AMBIGUOUS
        assert arity["--preserve-env"] not in ("zero", "optional", "required"), (
            "the sentinel must not collide with a real arity, or a caller that "
            "forgets to handle it silently accepts the entry"
        )
        # Not everything repeated is ambiguous: the SAME reading twice agrees.
        agree = _documented_arity("  -f FILE  a file\n  -f FILE  the same file\n")
        assert agree["-f"] == "required"

    def test_a_readable_parser_RESOLVES_ambiguity_rather_than_abstaining(self):
        """Abstention is for prose alone — it must not eat a tool we CAN measure.

        `xvfb-run` continues the description for `-a` onto a line reading only
        `--server-num`, which conflicts with that option's own definition line.
        That conflict is exactly what strictest-wins was introduced to absorb,
        so replacing strictest-wins with abstention would have silently dropped
        a correct entry from the lock. It does not, because the getopt spec is
        read first and outranks the prose.
        """
        if not shutil.which("xvfb-run"):
            pytest.skip("xvfb-run is not installed here — the conflict cannot be measured")
        from_help = _documented_arity(_help_text("xvfb-run") or "")
        assert from_help.get("--server-num") == _AMBIGUOUS, (
            "this test pins the prose CONFLICT; if the help stopped repeating the "
            "name, the hazard is gone and so is what this test protects"
        )
        resolved = {**from_help, **_parser_arity("xvfb-run")}
        assert resolved["--server-num"] == "required", (
            "the parser must settle what the prose could not"
        )

    def test_an_UNDECLARED_ambiguity_fails_instead_of_printing(self):
        """Guard the guard: the polarity that the previous shape got backwards.

        Abstention used to be a `print` from a passing test. MEASURED with CI's
        own flags: zero occurrences in the log and zero in the JUnit report, so a
        new ambiguous entry produced a green, silent build. An ambiguity the repo
        has not reviewed must therefore be RETURNED — and so fail the caller's
        assertion — not announced into a stream nobody reads.
        """
        assert _unexpected_ambiguities("xargs", {"-q": _AMBIGUOUS}, ["-q"]) == ["-q"]
        # And the declared one is allowed, by name, so the lock stays green on
        # the gap it has actually measured.
        assert _unexpected_ambiguities("sudo", {"-h": _AMBIGUOUS}, ["-h"]) == []

    def test_the_allowlist_is_anchored_to_an_exact_tool_and_option(self):
        """Guard the guard: an exemption that widens by prefix exempts the world.

        `-h` is excused for `sudo` and for nothing else, and only as the whole
        option — a substring or prefix match would quietly excuse `-h`-prefixed
        spellings on every tool in the table.
        """
        assert _unexpected_ambiguities("doas", {"-h": _AMBIGUOUS}, ["-h"]) == ["-h"], (
            "the allowlist must not leak across tools"
        )
        assert _unexpected_ambiguities("sudo", {"-host": _AMBIGUOUS}, ["-host"]) == ["-host"], (
            "the allowlist must match the whole option, not a prefix of it"
        )

    def test_the_caller_actually_FAILS_on_an_undeclared_ambiguity(self, monkeypatch):
        """Wire-check: the helper's verdict must reach an assertion, not a variable.

        The three tests above pin `_unexpected_ambiguities` itself, and all three
        stay green if the caller computes it and throws it away — verify-RED
        measured exactly that: stubbing `surprises = []` in the caller left the
        whole file passing. No real tool on this machine carries an undeclared
        ambiguity (that is the point), so the live parametrized case can never
        exercise the failing branch on its own.

        So inject the real defect Codex named — `sudo --preserve-env`, which the
        help declares both bare and as `=list` — into the table and drive the
        actual check. This is the acceptance bar kept as a test rather than run
        once: before the fix the same injection produced a green, silent build.
        """
        if _help_text("sudo") is None:
            pytest.skip("sudo is not installed here — the ambiguity cannot be measured")
        opts, *rest = sp._WRAPPER_SPEC["sudo"]
        monkeypatch.setitem(sp._WRAPPER_SPEC, "sudo", ({*opts, "--preserve-env"}, *rest))
        with pytest.raises(AssertionError) as caught:
            self.test_every_listed_option_documents_a_required_value("sudo")
        assert "--preserve-env" in str(caught.value), (
            "the failure must NAME the unsettleable option — an assertion that "
            "fires without saying which entry is ungraded sends the reader back "
            "to re-derive what the check already knew"
        )

    def test_a_gradeable_option_is_never_reported_as_an_ambiguity(self):
        """Negative control: only the sentinel is excused, not every listed option.

        Without this, a helper that returned its whole input would pass both
        tests above while destroying the check it feeds.
        """
        listed = ["-h", "-u", "-E"]
        arity = {"-h": _AMBIGUOUS, "-u": "required", "-E": "zero"}
        assert _unexpected_ambiguities("xargs", arity, listed) == ["-h"]
