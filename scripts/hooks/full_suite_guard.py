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
"""

from __future__ import annotations

import os
import sys

# Self-locate so the sibling imports resolve whether CC runs this as a script or
# it is imported as a module for tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload  # noqa: E402
from shell_parse import (  # noqa: E402
    _RUN_CARRIER_VALUE_FLAGS,
    Segment,
    _basename,
    analyze,
    has_trailing_override,
    is_pytest_invocation,
)

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
            if arg in ("-k", "-m") or arg.startswith(("-k", "-m", "--keyword")) or arg == "--pyargs":
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


def _carried_pytest_args(seg: Segment) -> list[str] | None:
    """Args after a literal `pytest` token inside an UNRESOLVED carrier, else None.

    The resolver models uv's option grammar to find the carried command, and that
    grammar is an OPEN set: a value-taking flag before `run` swallows `run`
    itself, so the carrier stays opaque and the segment resolves to `uv`.
    MEASURED: `uv --color always run pytest` was ALLOWED where `uv run pytest`
    blocks. Four such gaps were reported on this PR alone, which is the signature
    of enumerating someone else's CLI rather than a list that was merely short.

    So this does not extend the grammar. It asks a CLOSED question — does the
    literal token `pytest` appear in a carrier's argv — and hands the tokens after
    it to the SAME `_targets_specific_test` used on a resolved run. An unknown uv
    flag can no longer decide the verdict; at worst it costs one extra token
    before `pytest`, which this does not read.

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

    KNOWN RESIDUAL, safe direction: the `run` walk skips only flags it knows, so a
    literal `run` reached as an unlisted flag's value still ends the walk —
    `uv pip install --target run pytest` over-blocks. It is an install into a
    directory named `run`, it is refused rather than allowed, and `# full-suite-ok`
    clears it. Closing it needs pip's grammar, which is the open set this function
    exists to avoid.

    Returns None when the segment is not a carrier, carries no `run` subcommand,
    or carries no pytest token — `uv pip install requests` must stay allowed.
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
    while i < len(argv):
        tok = argv[i]
        if tok in _RUN_CARRIER_VALUE_FLAGS and "=" not in tok:
            i += 2  # `--with pytest` names a DEPENDENCY, not the command being run
            continue
        if _basename(tok).split("@", 1)[0] == "pytest":  # uv permits `pytest@8.3.5`
            return argv[i + 1 :]
        i += 1
    return None


def main() -> None:
    cmd = field(read_payload(), "command")
    if not cmd:
        return
    try:
        segments = analyze(cmd)
    except Exception:
        return  # parse failure → fail open; never wrongly block a legit command

    pytest_segs = [s for s in segments if is_pytest_invocation(s)]
    # Unresolved carriers are evaluated on the same rule, not waved through.
    carried = [a for a in (_carried_pytest_args(s) for s in segments) if a is not None]
    if not pytest_segs and not carried:
        return
    if any(has_trailing_override(s.raw, _OVERRIDE) for s in segments):
        return  # explicit opt-in to a local full/dir run

    # Block if ANY pytest run — resolved or carried — is non-targeted.
    resolved_ok = all(_targets_specific_test(_pytest_args(s)) for s in pytest_segs)
    carried_ok = all(_targets_specific_test(a) for a in carried)
    if resolved_ok and carried_ok:
        return

    print(
        "BLOCKED: full-suite / whole-directory pytest run. On this shared box the "
        "full suite duplicates CI and starves the live services (it was OOM-killed "
        "mid-run). Target a specific file (pytest tests/path/test_x.py) or a -k/-m "
        "selector, and let CI run the full suite on push. If you truly need the "
        f"local full run, append '# {_OVERRIDE}' to the command.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
