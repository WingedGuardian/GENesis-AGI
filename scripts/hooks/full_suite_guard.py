"""PreToolUse hook: warn when running full pytest suite without a specific path.

Running the entire test suite (5900+ tests, ~10 min) during iterative
development wastes time.  Targeted tests (specific file or directory)
run in seconds.  This hook detects bare `pytest` or `pytest -v` (no
test path) and warns the user.

Allowed without warning:
  - pytest tests/specific_file.py
  - pytest tests/some_dir/ -v
  - ruff check . && pytest -v  (pre-commit pattern — allowed)
  - Commands with explicit path arguments

Blocked with warning:
  - pytest -v
  - python -m pytest -q
  - pytest --tb=short  (flags only, no path)
"""

from __future__ import annotations

import json
import os
import re
import sys


# Flags that pytest accepts (non-exhaustive but covers common ones).
# Used to distinguish "pytest -v" (no path) from "pytest tests/foo.py -v".
_PYTEST_FLAGS = re.compile(
    r'^-[a-zA-Z]'      # short flags: -v, -x, -q, -s, etc.
    r'|^--[a-z]'       # long flags: --verbose, --tb=short, etc.
    r'|^[0-9]'         # bare numbers (unlikely but safe to skip)
)


def _is_full_suite(cmd: str) -> bool:
    """Return True if cmd runs pytest without a specific test path."""
    # Split on && and ; to handle chained commands
    parts = re.split(r'&&|;', cmd)
    for part in parts:
        part = part.strip()
        # Find the pytest invocation
        match = re.search(
            r'(?:\w+=\S*\s+)*(?:python3?\s+-m\s+)?pytest\b(.*)', part,
        )
        if not match:
            continue

        args_str = match.group(1).strip()
        if not args_str:
            return True  # bare "pytest" with no args

        # Split remaining args and check if any is a path (not a flag)
        args = args_str.split()
        has_path = False
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ('>', '2>&1', '|'):
                break  # redirection/pipe — stop parsing
            if arg.startswith('-'):
                # Flag — skip it (and its value if it takes one)
                if arg in ('-k', '-m', '--tb', '-p', '--timeout',
                           '--rootdir', '-c', '--co', '-W', '--override-ini'):
                    i += 1  # skip the next arg too (flag value)
            elif '/' in arg or arg.endswith('.py'):
                has_path = True
                break
            i += 1

        if not has_path:
            return True

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

    # Only care about commands that run pytest
    if not re.search(r'(?:^|&&|;|\|)\s*(?:\w+=\S*\s+)*(?:python3?\s+-m\s+)?pytest\b', cmd):
        return

    if _is_full_suite(cmd):
        msg = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    "WARNING: You are about to run the FULL test suite (5900+ tests, ~10 min). "
                    "During development, run only the specific test files for your changes. "
                    "The full suite should run ONCE at pre-commit time, not during iteration. "
                    "If this IS the pre-commit run, proceed. Otherwise, target your tests: "
                    "pytest tests/path/to/specific_test.py -v"
                ),
            }
        })
        print(msg)


if __name__ == "__main__":
    main()
