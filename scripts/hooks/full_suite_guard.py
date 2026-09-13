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
    Segment,
    analyze_checked,
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


def main() -> None:
    cmd = field(read_payload(), "command")
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
    # Cost of refusing both: 0 of 45,956 real commands reach either bound.
    if blind is not None and blind.bounds_induced and "pytest" in cmd:
        print(
            f"BLOCKED: this command {blind.cause}, so this guard cannot check whether "
            f"the pytest run inside it is targeted — and an untargeted full-suite run "
            f"starves the live services on this shared box. To proceed: {blind.hint}. "
            f"Run the pytest on its own line and it will be checked precisely; "
            f"'# full-suite-ok' still works on the parsed path.",
            file=sys.stderr,
        )
        sys.exit(2)

    pytest_segs = [s for s in segments if is_pytest_invocation(s)]
    if not pytest_segs:
        return
    if any(has_trailing_override(s.raw, _OVERRIDE) for s in segments):
        return  # explicit opt-in to a local full/dir run

    # Block if ANY pytest segment is a non-targeted (bare or directory) run.
    if all(_targets_specific_test(_pytest_args(s)) for s in pytest_segs):
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
