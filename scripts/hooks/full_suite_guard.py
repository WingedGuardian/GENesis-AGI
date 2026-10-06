"""PreToolUse hook: BLOCK a full-suite / whole-directory local pytest run.

Running a whole test directory locally on this shared box (let alone ``tests/``)
duplicates CI's authoritative full run and starves the live Genesis services —
which repeatedly OOM-killed the run mid-suite. Targeted local testing is a
SPECIFIC file (or a ``-k``/``-m`` selector); CI runs the full suite on every push.

This hook BLOCKS (exit 2) a pytest segment that targets no specific ``.py`` file
and no ``-k``/``-m`` selector — i.e. bare ``pytest`` / ``pytest -v`` or a bare
directory like ``tests/`` or ``tests/test_scripts/``. Detection routes through
``shell_parse.analyze`` (quote-aware), so a ``|pytest`` inside a quoted argument
is not misread as a run.

Allowed:
  - pytest tests/foo/test_bar.py            (a specific file / nodeid)
  - pytest tests/foo -k test_bar            (a -k/-m selector narrows the run)
  - pytest tests/foo.py --basetemp /wt      (a path-valued flag's value is not a target)
  - pytest tests/ -q  # full-suite-ok       (explicit override)
Blocked:
  - pytest -v                               (no path at all)
  - python -m pytest tests/test_scripts/    (a whole directory)
  - pytest tests/foo.py tests/              (a file + a whole dir still runs the dir)
  - systemd-run --user --scope pytest -n 4  (a wrapper does not hide the run; see
    _wrapped_command — also flock/watch/… and `genesis.hostmetrics run -- …`)
"""

from __future__ import annotations

import contextlib
import os
import shlex
import sys

# Self-locate so the sibling imports resolve whether CC runs this as a script or
# it is imported as a module for tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hook_input import degraded_exit, field, read_payload  # noqa: E402
except Exception:  # noqa: BLE001 — a missing NEW helper must block.
    if __name__ != "__main__":
        raise
    # REVERSE version skew: this guard may be newer than hook_input.py, in which case
    # nothing it could import can recover it — degraded_exit is the thing that is
    # missing. So fail closed locally. The exception is not rendered (even __str__ can
    # raise) and os._exit is used so a broken diagnostic stream cannot replace exit 2
    # during interpreter shutdown.
    try:
        sys.stderr.write(
            "GUARD DEGRADED (full_suite_guard): shared hook_input is incompatible; "
            "BLOCKING until the hook tree is repaired.\n"
        )
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — diagnostics cannot change fail direction.
        pass
    os._exit(2)

# DEGRADED-path mention set, defined ABOVE the guarded import so it survives that
# import failing. This guard exited 1 — NON-blocking — on a poisoned tree until now,
# and what it protects is a shared box: its own refusal message records that an
# untargeted run has repeatedly OOM-killed the suite and starved the live services.
#
# PRICE, stated rather than discovered: MEASURED over 74,282 real commands, 7,906
# (10.64%) mention pytest, and while the tree is broken every one of them is refused —
# including the TARGETED runs this guard normally allows. That is heavy, and it is
# still the right direction here: the refusal is loud and one repair away, where the
# fail-open is a box-wide stall. It is also why the background-pipe guard is NOT wired
# the same way: its only usable token is the pipe character, at 70.30%, which would
# leave the broken state unrepairable rather than merely inconvenient.
_DEGRADED_GATED = r"\bpytest\b"

try:
    from shell_parse import (  # noqa: E402
        _REPARSE_CARRIERS,
        _RUN_CARRIER_VALUE_FLAGS,
        Segment,
        _basename,
        analyze_checked,
        has_trailing_override,
        is_pytest_invocation,
        mentions,
    )
except Exception as _exc:  # noqa: BLE001 — exit 1 is NON-blocking; see degraded_exit.
    if __name__ != "__main__":
        raise
    degraded_exit("full_suite_guard", gated=_DEGRADED_GATED, exc=_exc)

try:  # noqa: E402
    import discarded_write
except Exception:  # noqa: BLE001 — GUARDED: an unguarded import failure would abort
    # module load → exit 1 → CC reads non-2 as NON-blocking → the full suite RUNS.
    discarded_write = None  # type: ignore[assignment]

_OVERRIDE = "full-suite-ok"

# pytest flags that consume the FOLLOWING token as their value, so that value is not
# mistaken for a positional. This matters because _targets_specific_test blocks on a
# bare-directory positional: a path/glob-valued flag's separate-token value
# (``--basetemp /wt``, ``--doctest-glob '*.py'``) would otherwise look like a directory
# (a WRONG block) or a .py file (a FAIL-OPEN). The path/glob-valued built-ins below are
# taken from ``pytest --help``. Non-exhaustive BY DESIGN: an UNLISTED value-flag whose
# separate-token value is a non-.py string falls back to a safe BLOCK (a false-block,
# overridable with ``# full-suite-ok``) — never a fail-open. The one fail-open risk is a
# value ENDING in .py, so the .py-glob flag ``--doctest-glob`` is listed explicitly.
_VALUE_FLAGS = {
    # config + common plugin flags (values are not paths)
    "-p",
    "-c",
    "-W",
    "-o",
    "-n",
    "--tb",
    "--timeout",
    "--override-ini",
    "--deselect",
    "--maxfail",
    # path / dir / glob-valued built-ins (pytest --help) — the false-block-prone ones
    "--rootdir",
    "--basetemp",
    "--confcutdir",
    "--config-file",
    "--ignore",
    "--ignore-glob",
    "--doctest-glob",
    "--junit-xml",
    "--junitxml",
    "--log-file",
}


def _pytest_args(seg: Segment) -> list[str]:
    """Positional+flag args AFTER the pytest command word (entrypoint stripped)."""
    argv = seg.argv
    if seg.exe == "pytest":
        return argv[1:]  # drop argv[0] entrypoint (may be a /path/to/pytest)
    for i, tok in enumerate(argv):
        if tok == "-m" and i + 1 < len(argv) and argv[i + 1] == "pytest":
            return argv[i + 2 :]
    return argv  # unreachable when is_pytest_invocation(seg) is True


def _targets_specific_test(args: list[str]) -> bool:
    """True if this pytest arg list is a TARGETED run (allow), False for a bare or
    directory-touching run (block).

    Targeted = a -k/-m/--pyargs selector, OR at least one specific .py file/nodeid
    and NO bare-directory positional. A file mixed with a directory
    (``pytest tests/foo.py tests/``) still runs the WHOLE directory — pytest UNIONS
    positionals, so a named file does not narrow the run; it remains the OOM-inducing
    full run this guard exists to stop, and therefore blocks. Distinguishing a real
    directory positional from a value-flag's path value (``--basetemp /wt``) is the job
    of ``_VALUE_FLAGS`` (which consumes the value); an unlisted value-flag falls back to
    a safe BLOCK, never a fail-open (see the ``_VALUE_FLAGS`` note).
    """
    has_selector = False
    has_file = False
    has_dir = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            # a -k/-m selector (separate value, =form, or glued) narrows the run
            if (
                arg in ("-k", "-m")
                or arg.startswith(("-k", "-m", "--keyword"))
                or arg == "--pyargs"
            ):
                has_selector = True
            elif arg in _VALUE_FLAGS:
                i += 2  # skip the flag AND its value
                continue
            i += 1
            continue
        # A real .py file or nodeid — NOT a mere substring, so a directory like
        # tests/.pytest_cache/ or foo.python_stuff/ counts as a directory, not a file.
        if arg.endswith(".py") or ".py::" in arg:
            has_file = True
        else:
            has_dir = True  # a bare directory / non-file positional path
        i += 1
    return has_selector or (has_file and not has_dir)


#: Front-ends that can carry another command. If the resolver could not see PAST
#: one of these, the segment is UNRESOLVED — which is not the same as clean.
_CARRIER_EXES = frozenset({"uv", "uvx", "poetry", "hatch", "pdm", "pipenv", "rye"})

#: Carriers this set deliberately LACKS, with the reason each is elsewhere's
#: problem — test_carrier_sets fails when a union member is absent without a
#: recorded reason (#2232), so an omission can no longer pose as a decision.
_NOT_A_RUN_FRONTEND = (
    "not a package-manager `run` front-end — this set gates the literal "
    "`run` subcommand only"
)
_REPARSE_JUDGED_ELSEWHERE = (
    "a re-parse launcher (shell_parse._REPARSE_CARRIERS) — not a `run` front-end; "
    "the command it carries is judged by _wrapped_command instead"
)
_CARRIER_EXCLUDES: dict[str, str] = {
    # re-parse launchers — taken from the set itself, so the reason stays true
    **{name: _REPARSE_JUDGED_ELSEWHERE for name in _REPARSE_CARRIERS},
    **{
        name: _NOT_A_RUN_FRONTEND
        for name in (
            # remote / argv-visible carriers — worktree_cwd_guard._CARRIER_NAMES
            "ssh", "find", "parallel", "docker", "xargs",
            # nested shells — destructive_command_guard._NESTED_SHELLS
            "bash", "sh", "dash", "zsh", "ksh", "ash",
        )
    },
}


def _carried_pytest_args(seg: Segment) -> list[str] | None:
    """Args after a carried pytest executable inside an UNRESOLVED carrier, else None.

    The resolver models uv's option grammar to find the carried command, and that
    grammar is an OPEN set: a value-taking flag before `run` swallows `run`
    itself, so the carrier stays opaque and the segment resolves to `uv`.
    MEASURED: `uv --color always run pytest` was ALLOWED where `uv run pytest`
    blocks. Four such gaps were reported on this PR alone, which is the signature
    of enumerating someone else's CLI rather than a list that was merely short.

    So this does not extend the grammar. It identifies the first command token
    after the literal ``run`` and hands its following tokens to the SAME
    `_targets_specific_test` used on a resolved run. Scanning every later token
    is incorrect: ``uv --color always run echo pytest`` runs ``echo``, while
    ``pytest`` is only its argument.

    The scan starts AFTER the `run` literal, because a `pytest` token ahead of it
    is a package NAME, not an invocation. MEASURED on this PR's own tree: scanning
    the whole argv blocked 18 install/inspect commands — `uv pip install pytest`,
    `uv add pytest`, `poetry add pytest`, `pipenv install pytest`, `pdm remove
    pytest`, `uv pip show pytest` — with a message telling the user to target a
    specific file, advice that means nothing for an install. Requiring the literal
    keeps the whole fail-open set closed: `uv --color always run pytest` and
    `uv --cache-dir /tmp/c run pytest` both carry `run` AHEAD of the token, which
    is exactly why the closed question beats modelling the flag grammar. `uvx`
    takes the command directly and has no subcommand to require.

    Both walks skip a value-flag's VALUE, using the SAME list the resolver walks
    with. That is one grammar dependency back, taken deliberately, because the
    unlisted-flag direction of this list is the safe one: a missing entry costs an
    extra token read (an over-block, overridable), while the list's one dangerous
    direction — a BOOLEAN flag wrongly listed — is the failure `--isolated` already
    taught this module, and is guarded there. Without the skip, a package NAME
    passed to a flag was read as the command: MEASURED, `uv --color always run
    --with pytest ruff check .` (a ruff run) and `uv --color always run --with
    pytest pytest tests/foo.py` (a correctly TARGETED run) both blocked.

    SECOND KNOWN RESIDUAL, also safe direction, and it is the price of the
    confidence rule below: once the walk meets an option it cannot size, it stops
    claiming to know which bare word is the command and looks for a `pytest`
    token among the rest. So `uvx --allow-insecure-host h echo pytest` — which
    runs `echo` — is refused. That is the same over-read the docstring above
    rejects for the CONFIDENT case, accepted here only because confidence is
    gone: the alternative is committing to the flag's value and allowing the
    whole-suite run this function exists to stop. It costs a refusal
    `# full-suite-ok` clears.

    It is narrower than it first looks, and the narrowing was MEASURED rather
    than assumed — the first example written here was wrong and a test caught it.
    The option must be unknown to BOTH this list and the resolver's `uvx` wrapper
    spec. A flag only the resolver lacks (`--directory`) leaves the segment on
    the carrier, but THIS walk still sizes it, stays confident, and reads `echo`
    as the command exactly as before.

    KNOWN RESIDUAL, safe direction: the `run` walk skips only flags it knows, so a
    literal `run` reached as an unlisted flag's value still ends the walk —
    `uv pip install --target run pytest` over-blocks. It is an install into a
    directory named `run`, it is refused rather than allowed, and `# full-suite-ok`
    clears it. Closing it needs pip's grammar, which is the open set this function
    exists to avoid.

    Returns None when the segment is not a carrier, carries no `run` subcommand,
    or carries a command other than pytest — `uv pip install requests` must stay
    allowed.
    """
    if _basename(seg.exe) not in _CARRIER_EXES:
        return None  # resolved to a real command (or not a carrier at all)
    argv = seg.argv
    i = 1
    if _basename(seg.exe) != "uvx":
        # `uv pip install pytest` installs pytest, it does not run it — only a
        # `run` subcommand carries a command. (`uvx` takes the command directly.)
        while i < len(argv):
            tok = argv[i]
            if tok in _RUN_CARRIER_VALUE_FLAGS and "=" not in tok:
                i += 2  # a flag's value is never the subcommand
                continue
            if tok == "run":
                i += 1
                break
            i += 1
        else:
            return None
    confident = True
    while i < len(argv):
        tok = argv[i]
        if tok in _RUN_CARRIER_VALUE_FLAGS and "=" not in tok:
            i += 2  # `--with pytest` names a DEPENDENCY, not the command being run
            continue
        if tok.startswith("-"):
            # An option of unknown arity. From here the walk can no longer say
            # WHICH bare word is the command, because the next one may be this
            # flag's value — so it stops treating the first bare word as the
            # answer. Committing to it is the fail-OPEN reading: MEASURED,
            # `uvx --directory /tmp pytest` resolved `tmp`, concluded "not
            # pytest" and exited 0 where `uvx pytest` exits 2.
            confident = confident and "=" in tok
            i += 1
            continue
        if _basename(tok).split("@", 1)[0] != "pytest":  # uv permits `pytest@8.3.5`
            if confident:
                return None  # the command is known, and it is not pytest
            i += 1  # unsure which token is the command — keep looking for one
            continue
        return argv[i + 1 :]
    return None


#: Value-taking options of `genesis.hostmetrics run` — this repo's own argparse, so
#: a CLOSED set — skipped so that a value such as `--name pytest` is never read as
#: the command the wrapper launches.
_HOSTMETRICS_VALUE_FLAGS = frozenset(
    {
        "--name",
        "--ram",
        "--cpu",
        "--disk",
        "--cpu-window",
        "--wait-until-fits",
        "--slice",
        "--approved-over-line",
    }
)
#: Wrappers inside wrappers are followed this deep; past it a pytest mention is
#: judged untargeted (refused), never waved through.
_MAX_WRAP_DEPTH = 8


def _hostmetrics_run_start(argv: list[str]) -> int | None:
    """Index just past `run` in `<python> -m genesis.hostmetrics run …`, else None."""
    for i in range(len(argv) - 2):
        if argv[i] == "-m" and argv[i + 1] == "genesis.hostmetrics":
            return i + 3 if argv[i + 2] == "run" else None
    return None


#: The other options of `genesis.hostmetrics run` (boolean). Together with
#: `_HOSTMETRICS_VALUE_FLAGS` this is its whole grammar; an option outside both is
#: one the walk cannot size (an abbreviation, a flag added later), and the walk
#: stops trusting its own reading of which word is the command.
_HOSTMETRICS_BOOL_FLAGS = frozenset({"--json", "--no-host", "--assume-default", "-h", "--help"})


def _pytest_candidates(tokens: list[str]) -> list[str]:
    """Every reading of `tokens` that could start a carried pytest run.

    A token named pytest starts one; so does a quoted string that mentions pytest,
    joined with the tokens after it, because `eval` and `watch` join their
    arguments into one command line (`eval 'pytest a.py;' 'pytest -n 4'` runs both).
    """
    out: list[str] = []
    for j, tok in enumerate(tokens):
        if _basename(tok).split("@", 1)[0] in ("pytest", "py.test"):
            out.append(shlex.join(tokens[j:]))
        elif any(c.isspace() for c in tok) and mentions(tok, "pytest"):
            out.append(" ".join(tokens[j:]))
    return out


def _wrapped_command(seg: Segment) -> list[str] | None:
    """Every command text a wrapper may carry, or None when `seg` is not a wrapper.

    The resolver stops at these wrappers, so without this a pytest inside one never
    reaches `is_pytest_invocation`. MEASURED before this function existed:
    `systemd-run --user --scope pytest -n 4`, `flock /tmp/l pytest` and
    `<venv python> -m genesis.hostmetrics run … -- pytest -n 4` all exited 0.

    EVERY candidate is returned and judged, never just the first: an earlier
    version stopped at the first match and allowed `eval 'pytest a.py;' 'pytest
    -n 4'` and `script -c "pytest -n 4" -- /dev/null` (MEASURED, review).

    Two kinds, read differently:
    - `genesis.hostmetrics run` is this repo's own CLI (`_HOSTMETRICS_VALUE_FLAGS`
      + `_HOSTMETRICS_BOOL_FLAGS`). After `--` the command is exact. Otherwise it is
      the first bare word no option consumes — until an option the walk cannot size
      appears (an abbreviation argparse would accept, a newer flag), after which
      every pytest token in the rest is a candidate instead.
    - A re-parse launcher (`systemd-run`, `flock`, `watch`, … —
      `shell_parse._REPARSE_CARRIERS`) has a grammar this repo deliberately does
      not model. The tail after a `--` is one candidate; every pytest token, and
      every quoted string mentioning pytest, BEFORE that `--` is another (a
      launcher like `script -c CMD -- FILE` carries its command ahead of it).

    This over-reads in the safe direction only: any argument spelled pytest — an
    option value (`--unit pytest`), or the carried command's own argument
    (`pip install pytest`) — can refuse a command that `# full-suite-ok` then
    clears. It never allows an untargeted run that the bare form refuses.
    """
    argv = seg.argv
    exe = _basename(seg.exe)
    if exe.startswith("python"):
        i = _hostmetrics_run_start(argv)
        if i is None:
            return None
        rest = argv[i:]
        if "--" in rest:
            return [shlex.join(rest[rest.index("--") + 1 :])]
        k = 0
        while k < len(rest):
            tok = rest[k]
            if tok in _HOSTMETRICS_VALUE_FLAGS:
                k += 2
                continue
            if tok.split("=", 1)[0] in _HOSTMETRICS_VALUE_FLAGS | _HOSTMETRICS_BOOL_FLAGS:
                k += 1
                continue
            if tok.startswith("-"):
                return _pytest_candidates(rest[k:])  # an option it cannot size
            return [shlex.join(rest[k:])]
        return []
    if exe not in _REPARSE_CARRIERS:
        return None
    rest = argv[1:]
    if "--" in rest:
        cut = rest.index("--")
        return [shlex.join(rest[cut + 1 :]), *_pytest_candidates(rest[:cut])]
    return _pytest_candidates(rest)


def _wrapped_pytest_args(segments: list[Segment], depth: int = 0) -> list[list[str]]:
    """Arg lists of the pytest runs that wrappers among `segments` carry.

    Each carried command is parsed with the same `analyze_checked` and judged by
    the same rules as a top-level one, so a wrapper changes nothing about what
    counts as targeted — including the `# full-suite-ok` override written inside
    it. An unreadable carried command that mentions pytest yields an EMPTY arg
    list — untargeted — as does nesting past `_MAX_WRAP_DEPTH`.
    """
    runs: list[list[str]] = []
    for seg in segments:
        for text in _wrapped_command(seg) or []:
            if not text:
                continue
            if depth >= _MAX_WRAP_DEPTH:
                if mentions(text, "pytest"):
                    runs.append([])
                continue
            try:
                inner, blind = analyze_checked(text)
            except Exception:  # noqa: BLE001 — unreadable carried text is judged, not skipped
                if mentions(text, "pytest"):
                    runs.append([])
                continue
            if blind is not None and blind.bounds_induced and mentions(text, "pytest"):
                runs.append([])
                continue
            if any(has_trailing_override(s.raw, _OVERRIDE) for s in inner):
                continue  # the override, written inside the wrapper, counts as at top level
            runs += [_pytest_args(s) for s in inner if is_pytest_invocation(s)]
            runs += [a for a in (_carried_pytest_args(s) for s in inner) if a is not None]
            runs += _wrapped_pytest_args(inner, depth + 1)
    return runs


def main() -> None:
    cmd = field(read_payload(), "command")
    if discarded_write is not None:
        with contextlib.suppress(Exception):  # not run_guard-wrapped: a raise here exits 1 = NON-blocking
            discarded_write.remember(cmd)
    if not cmd:
        return
    try:
        segments, blind = analyze_checked(cmd)
    except Exception:
        return  # parse failure → fail open; never wrongly block a legit command

    # A parse cut short by one of shell_parse's BOUNDS is not evidence there is no
    # pytest run in here. This guard's fail-open posture is about a command it cannot
    # read AT ALL; a bound does not raise, it quietly returns fewer segments, so
    # reading that as "no pytest" turned a refusal into an allow. MEASURED before this
    # call was switched: a bare `pytest` nested 9 deep went from refused to allowed.
    #
    # `untokenizable` is deliberately EXCLUDED — it predates the bounds, this guard
    # already allowed those, and failing closed on it would newly refuse 161 of 3,222
    # real pytest-mentioning commands (against 0 for the bounds). Restore what the
    # bound took; do not widen under cover of the same edit.
    #
    # BOTH bounds refuse. There is no per-axis severity to consult, for the reason
    # documented at length in git_discard_guard._clean_violation. This guard's only
    # verdicts are BLOCK and ALLOW; it cannot ask. For a guard with no third option,
    # softening an axis is not "a lighter verdict", it is a silent permit, and the
    # sibling layer that was supposed to cover the softened case did not.
    # Cost of refusing both bounds: 0 of 45,956 real commands reach either. A line
    # continuation is ordinary input and takes this branch too — its cost is
    # measured at `shell_parse._BLIND_CONTINUATION`.
    # `mentions`, not the raw text: a line continuation is a bounds-type blind
    # spot too, and when it falls inside the word the raw text no longer spells the
    # name — the one case where this branch fires without it.
    if blind is not None and blind.bounds_induced and mentions(cmd, "pytest"):
        print(
            f"BLOCKED: this command {blind.cause}, so this guard cannot check whether "
            f"the pytest run inside it is targeted — and an untargeted full-suite run "
            f"starves the live services on this shared box. To proceed: {blind.hint}. "
            f"Run the pytest on its own line and it will be checked precisely; "
            f"'# full-suite-ok' still works on the parsed path.",
            file=sys.stderr,
        )
        if discarded_write is not None:
            with contextlib.suppress(Exception):  # not run_guard-wrapped: a raise here exits 1 = NON-blocking
                discarded_write.warn()
        sys.exit(2)

    pytest_segs = [s for s in segments if is_pytest_invocation(s)]
    # Unresolved carriers are evaluated on the same rule, not waved through.
    carried = [a for a in (_carried_pytest_args(s) for s in segments) if a is not None]
    # So are runs a wrapper carries (systemd-run, flock, genesis.hostmetrics run, …).
    wrapped = _wrapped_pytest_args(segments)
    if not pytest_segs and not carried and not wrapped:
        return
    if any(has_trailing_override(s.raw, _OVERRIDE) for s in segments):
        return  # explicit opt-in to a local full/dir run

    # Block if ANY pytest run — resolved or carried — is non-targeted.
    resolved_ok = all(_targets_specific_test(_pytest_args(s)) for s in pytest_segs)
    carried_ok = all(_targets_specific_test(a) for a in carried)
    wrapped_ok = all(_targets_specific_test(a) for a in wrapped)
    if resolved_ok and carried_ok and wrapped_ok:
        return

    print(
        "BLOCKED: full-suite / whole-directory pytest run. On this shared box the "
        "full suite duplicates CI and starves the live services (it was OOM-killed "
        "mid-run). Target a specific file (pytest tests/path/test_x.py) or a -k/-m "
        "selector, and let CI run the full suite on push. If you truly need the "
        f"local full run, append '# {_OVERRIDE}' to the command.",
        file=sys.stderr,
    )
    if discarded_write is not None:
        with contextlib.suppress(Exception):  # not run_guard-wrapped: a raise here exits 1 = NON-blocking
            discarded_write.warn()
    sys.exit(2)


if __name__ == "__main__":
    main()
