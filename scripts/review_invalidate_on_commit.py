#!/usr/bin/env python3
"""PostToolUse hook (Bash): invalidate review marker after successful git commit.

Every commit must be preceded by a fresh review. This hook clears the marker
after any successful git commit, so the next commit will require review again.

Reads the CC PostToolUse payload from stdin (via hook_input) — the git
command from tool_input, the outcome from tool_response.

Exit codes:
  0 = always (PostToolUse hooks cannot block)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import field, read_payload, tool_response  # noqa: E402

# review_state lives in scripts/ (this file's own dir).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_state import clear_marker  # noqa: E402

_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")


def _extract_working_dir(command: str) -> str | None:
    """Extract the worktree cwd from a leading ``cd <path> && ...`` so the marker
    cleared matches the per-worktree marker that authorized this commit."""
    import os

    m = re.match(r"^cd\s+([^\s&|;]+)", command)
    if not m:
        return None
    path = os.path.expanduser(m.group(1))
    return path if os.path.isdir(path) else None


def main() -> None:
    payload = read_payload()

    # Check if the tool input contained a git commit command
    command = field(payload, "command")
    if not _COMMIT_PATTERN.search(command):
        sys.exit(0)

    # Only invalidate on successful commits (exit code 0)
    try:
        result = tool_response(payload)
        # CC wraps Bash results — check for error indicators
        _stdout = result.get("stdout", "")  # noqa: F841
        _stderr = result.get("stderr", "")  # noqa: F841
        # A successful git commit prints to stdout with the branch and hash
        # A failed commit (e.g. pre-commit hook) has non-zero exit
        if "error" in result and result["error"]:
            sys.exit(0)  # Commit failed, don't invalidate
    except (json.JSONDecodeError, AttributeError):
        pass  # Can't parse result — be conservative, invalidate anyway

    # Clear the per-worktree review marker (matching the cwd that authorized
    # this commit) so the next commit requires a fresh review.
    clear_marker(cwd=_extract_working_dir(command))

    sys.exit(0)


if __name__ == "__main__":
    main()
