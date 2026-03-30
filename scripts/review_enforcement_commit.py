#!/usr/bin/env python3
"""PreToolUse hook (Bash): block commits without review.

Two enforcement rules:
1. Block ALL commits directly to main — always require a branch.
2. Block commits on branches if review marker is not current.

Reads CLAUDE_TOOL_INPUT from environment (set by CC hook framework).

Exit codes:
  0 = allow (tool proceeds)
  2 = deny (tool blocked)

Output format for denial:
  JSON with hookSpecificOutput.permissionDecision = "deny"
"""

from __future__ import annotations

import json
import os
import re
import sys

# Pattern to detect git commit commands (but not git commit --amend, etc. — those
# are also commits and should be blocked)
_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")
_ADD_PATTERN = re.compile(r"\bgit\s+add\b")


def _extract_working_dir(command: str) -> str | None:
    """Extract the effective working directory from a Bash command.

    When CC runs in a worktree, commands typically start with
    'cd /path/to/worktree && ...'.  Extract that path so git commands
    in review_state.py run in the correct directory.
    """
    m = re.match(r"^cd\s+([^\s&|;]+)", command)
    if not m:
        return None
    path = os.path.expanduser(m.group(1))
    return path if os.path.isdir(path) else None


def main() -> None:
    # Parse tool input
    tool_input_raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
    if not tool_input_raw:
        sys.exit(0)  # No input, allow

    try:
        tool_input = json.loads(tool_input_raw)
    except json.JSONDecodeError:
        sys.exit(0)  # Can't parse, allow

    command = tool_input.get("command", "")
    if not _COMMIT_PATTERN.search(command):
        sys.exit(0)  # Not a commit, allow

    # Detect worktree: extract working directory from 'cd /path && git commit'
    cwd = _extract_working_dir(command)

    # Import review_state from same directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)

    try:
        from review_state import get_current_branch, has_code_changes, has_valid_review_marker, is_review_current
    except ImportError:
        # If review_state.py is missing, fail open — don't block
        sys.exit(0)

    branch = get_current_branch(cwd=cwd)

    # Rule 1: Block commits on main
    if branch in ("main", "master"):
        _deny(
            "BLOCKED: Direct commits to main are not allowed. "
            "Create a branch first: git checkout -b <scope>/<description>"
        )
        return

    # Rule 2: Block commits without review (on branches)
    # Race condition: when command is "git add X && git commit", nothing is
    # staged yet at hook time because git add hasn't run. Detect git add in
    # the same command chain — if present, require the marker file to exist
    # (the PostToolUse hook deletes it after every commit).
    stages_in_same_command = bool(_ADD_PATTERN.search(command))

    if stages_in_same_command:
        # Can't check diff hash (staging hasn't happened yet).
        # Just require the marker file to exist and not be expired.
        if not has_valid_review_marker():
            _deny(
                "BLOCKED: No current review marker. "
                "Run /review and dispatch the superpowers:code-reviewer agent first, "
                "then run: python3 scripts/review_state.py mark --agent-output ~/.genesis/last_code_review.txt"
            )
            return
    elif has_code_changes(cwd=cwd) and not is_review_current():
        _deny(
            "BLOCKED: Code changes exist without review. "
            "Run /review and dispatch the superpowers:code-reviewer agent first, "
            "then run: python3 scripts/review_state.py mark --agent-output ~/.genesis/last_code_review.txt"
        )
        return

    # All checks passed — allow
    sys.exit(0)


def _deny(message: str) -> None:
    """Output denial JSON and exit."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "additionalContext": message,
        }
    }
    json.dump(output, sys.stdout)
    sys.exit(0)  # Exit 0 — hook succeeded (tool is denied via JSON, not exit code)


if __name__ == "__main__":
    main()
