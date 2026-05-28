#!/usr/bin/env python3
"""PreToolUse hook (Bash): block git worktree remove on the current working directory.

Removing a worktree that is the shell's CWD bricks the entire session —
every subsequent Bash command fails with "Path does not exist" because the
shell's persisted working directory no longer exists.

Incident: 2026-05-27. Session permanently lost Bash capability after
deleting the worktree it was running in.

Stdlib-only. Fail-open on parse errors.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Matches 'git worktree remove' with optional flags
_WORKTREE_REMOVE = re.compile(
    r"\bgit\s+worktree\s+remove\b"
)


def _extract_worktree_targets(cmd: str) -> list[str]:
    """Extract target paths from git worktree remove commands."""
    targets: list[str] = []
    for match in _WORKTREE_REMOVE.finditer(cmd):
        rest = cmd[match.end():]
        # Skip flags, grab paths
        for token in rest.split():
            token = token.strip("'\"")
            if not token:
                continue
            if token.startswith("-"):
                continue  # skip flags like --force
            # This looks like a path
            targets.append(token)
            break  # git worktree remove takes one path
    return targets


def _resolve_path(path: str) -> str:
    """Resolve a path to its absolute, real form."""
    expanded = os.path.expanduser(path)
    return os.path.realpath(os.path.abspath(expanded))


def main() -> int:
    try:
        raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if not raw:
            return 0

        data = json.loads(raw)
        cmd = data.get("command", "")
        if not cmd:
            return 0

        if not _WORKTREE_REMOVE.search(cmd):
            return 0

        # Get the current working directory
        cwd = os.getcwd()
        cwd_real = os.path.realpath(cwd)

        targets = _extract_worktree_targets(cmd)
        for target in targets:
            target_real = _resolve_path(target)

            # Block if CWD is inside the target worktree
            # (CWD equals target or is a subdirectory of it)
            if cwd_real == target_real or cwd_real.startswith(target_real + "/"):
                print(
                    f"BLOCKED: Cannot remove worktree '{target}' — "
                    f"it is your current working directory.",
                    file=sys.stderr,
                )
                print(
                    "This would brick the session (every Bash command would "
                    "fail with 'Path does not exist').",
                    file=sys.stderr,
                )
                print(
                    "Fix: cd to the main repo first, then remove the worktree.",
                    file=sys.stderr,
                )
                return 2

    except (json.JSONDecodeError, KeyError, OSError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
