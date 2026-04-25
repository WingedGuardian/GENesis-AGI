"""PreToolUse hook: block concurrent pytest runs.

Fires on every Bash tool call. If the command contains a pytest invocation
AND a pytest process is already running, blocks with exit 2.

Catches all invocation patterns:
  - pytest ...
  - python -m pytest ...
  - python3 -m pytest ...
  - Chained: ruff check . && pytest ...
  - Any command containing 'pytest' as a standalone word
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def _command_runs_pytest(cmd: str) -> bool:
    """Check if a shell command will invoke pytest (any variant)."""
    # Match pytest as a standalone command or as a module invocation.
    # Covers: pytest, python -m pytest, python3 -m pytest, chained commands,
    # and env-var prefixed invocations (PYTHONPATH=src pytest ...).
    # Does NOT match: "grep pytest", "cat pytest.ini", etc. — requires word boundary.
    # The (?:\w+=\S*\s+)* handles env var assignments before the command.
    return bool(re.search(
        r'(?:^|&&|;|\|)\s*(?:\w+=\S*\s+)*(?:python3?\s+-m\s+)?pytest\b', cmd
    ))


def _pytest_already_running() -> bool:
    """Check if any pytest process is currently running (excluding this hook)."""
    my_pid = os.getpid()
    try:
        # pgrep -f uses POSIX extended regex (NOT PCRE — no \b).
        # Match "pytest" followed by space, end-of-string, or common flag chars.
        result = subprocess.run(
            ["pgrep", "-f", "pytest( |$)"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid != my_pid:
                return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def main() -> None:
    tool_input = os.environ.get("CLAUDE_TOOL_INPUT", "{}")
    try:
        data = json.loads(tool_input)
    except json.JSONDecodeError:
        return

    cmd = data.get("command", "")
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
