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
  - pytest tests/ -q  # full-suite-ok       (explicit override)
Blocked:
  - pytest -v                               (no path at all)
  - python -m pytest tests/test_scripts/    (a whole directory)
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
    analyze,
    has_trailing_override,
    is_pytest_invocation,
)

_OVERRIDE = "full-suite-ok"

# pytest flags that consume the FOLLOWING token as their value (so that value is
# not a positional path). Non-exhaustive but covers the common ones; an unlisted
# value-flag only risks fail-OPEN (not blocking), never a wrong block.
_VALUE_FLAGS = {
    "-p",
    "--tb",
    "--timeout",
    "--rootdir",
    "-c",
    "-W",
    "--override-ini",
    "-o",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--maxfail",
    "-n",
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
    """True if args name a specific ``.py`` file/nodeid OR carry a -k/-m selector.

    False for a bare run or directory-only args — the case this hook blocks.
    """
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            # a -k/-m selector (separate value, =form, or glued) narrows the run
            if arg in ("-k", "-m") or arg.startswith(("-k", "-m", "--keyword")):
                return True
            if arg == "--pyargs":  # --pyargs pkg.mod → import-path targeting = a targeted run
                return True
            if arg in _VALUE_FLAGS:
                i += 2  # skip the flag AND its value
                continue
            i += 1
            continue
        # A real .py file or nodeid — NOT a mere substring, so a directory like
        # tests/.pytest_cache/ or foo.python_stuff/ does not defeat the block.
        if arg.endswith(".py") or ".py::" in arg:
            return True
        # a bare directory / non-file positional path → not targeted; keep scanning
        i += 1
    return False


def main() -> None:
    cmd = field(read_payload(), "command")
    if not cmd:
        return
    try:
        segments = analyze(cmd)
    except Exception:
        return  # parse failure → fail open; never wrongly block a legit command

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
