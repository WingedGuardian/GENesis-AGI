"""PreToolUse hook: block concurrent pytest runs.

Fires on every Bash tool call. If the command contains a pytest invocation
AND a pytest process is already running, blocks with exit 2.

Catches all invocation patterns:
  - pytest ...
  - python -m pytest ...
  - python3 -m pytest ...
  - Chained: ruff check . && pytest ...
  - Any command containing 'pytest' as a standalone word

Running-process detection scans /proc cmdlines directly (2026-08 rewrite):
the old `pgrep -f "pytest( |$)"` matched ANY process whose command line merely
CONTAINED the word — `grep pytest x`, `tail -f pytest.log`, an editor on a
pytest file — falsely blocking a legitimate first test run. A process counts
only when it IS pytest: argv[0] basename == pytest, or a python interpreter
invoked with `-m pytest`.
"""

from __future__ import annotations

import os
import re
import sys

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload  # noqa: E402


def _command_runs_pytest(cmd: str) -> bool:
    """Check if a shell command will invoke pytest (any variant)."""
    # Match pytest as a standalone command or as a module invocation.
    # Covers: pytest, python -m pytest, python3 -m pytest, chained commands,
    # and env-var prefixed invocations (PYTHONPATH=src pytest ...).
    # Does NOT match: "grep pytest", "cat pytest.ini", etc. — requires word boundary.
    # The (?:\w+=\S*\s+)* handles env var assignments before the command.
    return bool(re.search(r"(?:^|&&|;|\|)\s*(?:\w+=\S*\s+)*(?:python3?\s+-m\s+)?pytest\b", cmd))


def _argv_is_pytest(argv: list[str]) -> bool:
    """Whether a process argv IS a pytest run (not a mere textual mention).

    True only for:
      * argv[0] whose basename is exactly ``pytest`` (venv/system entrypoint);
      * a python interpreter (basename starts with ``python``) whose args
        contain the adjacent pair ``-m pytest``.
    Everything else — ``grep pytest …``, ``tail -f pytest.log``, an editor on
    ``pytest.ini`` — is NOT a pytest process. Pure function; hermetically
    testable.
    """
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if base == "pytest":
        return True
    if base.startswith("python"):
        # `python -m pytest …`
        for i, tok in enumerate(argv[1:-1], start=1):
            if tok == "-m" and argv[i + 1] == "pytest":
                return True
        # A venv console-script launched via its Python shebang commonly appears
        # as `python /venv/bin/pytest …` (the repo's normal invocation). The
        # entrypoint is the FIRST non-flag argument; recognize it by basename so
        # a real running suite is not missed (Codex P2). `-c`/`-m` values are
        # skipped so their operand can't be misread as the script.
        i = 1
        while i < len(argv):
            tok = argv[i]
            if tok in ("-m", "-c", "-W", "-X"):  # flags that consume the next token
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            # First non-flag = the script to run. A console-script entrypoint is
            # a PATH (`/venv/bin/pytest`); require the slash so a bare
            # `python pytest` (running a local file named pytest, not a suite)
            # is not misread as a run.
            return "/" in tok and os.path.basename(tok) == "pytest"
    return False


def _pytest_already_running() -> bool:
    """Scan /proc for a live pytest process (excluding this hook's own chain).

    Reads each /proc/<pid>/cmdline (NUL-separated argv — a single syscall per
    process, same approach as worktree_cwd_guard's cwd scan). Processes that
    vanish mid-scan are skipped. Fail-open on any scan error: this guard is a
    resource-contention convenience, not an irreversible-action gate.
    """
    exclude = {os.getpid(), os.getppid()}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue  # process vanished or unreadable — skip
        argv = [t.decode("utf-8", "replace") for t in raw.split(b"\x00") if t]
        if _argv_is_pytest(argv):
            return True
    return False


def main() -> None:
    cmd = field(read_payload(), "command")
    if not cmd:
        return

    if not _command_runs_pytest(cmd):
        return

    if _pytest_already_running():
        print(
            "BLOCKED: A pytest process is already running. "
            "Wait for it to finish before launching another test run. "
            "Concurrent test suites cause resource contention and take "
            "3-5x longer than sequential runs.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
