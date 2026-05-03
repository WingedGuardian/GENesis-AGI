#!/usr/bin/env python3
"""PreToolUse hook: block git push/merge to main without user approval.

Catches all variations of pushing to or merging into the main branch:
- git push (bare, when on main)
- git push origin main
- git push -u origin main
- git merge <branch> (when on main)
- gh pr merge (without --admin — requires explicit user approval flag)

Stdlib-only. Fail-open on parse errors (don't block legitimate work).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _current_branch() -> str | None:
    """Get current git branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _get_push_remote_and_branch(cmd: str) -> tuple[str | None, str | None]:
    """Parse git push command to determine target remote and branch.

    Returns (remote, branch) or (None, None) if can't determine.
    """
    parts = cmd.split()
    # Find 'push' position
    try:
        push_idx = parts.index("push")
    except ValueError:
        return None, None

    # Skip flags after 'push'
    args = []
    i = push_idx + 1
    while i < len(parts):
        if parts[i].startswith("-"):
            # Skip flags and their arguments
            if parts[i] in ("-u", "--set-upstream", "--force-with-lease"):
                i += 1  # These don't take a separate argument in this context
            i += 1
            continue
        args.append(parts[i])
        i += 1

    if len(args) == 0:
        # Bare 'git push' — pushes current branch to its upstream
        return "upstream", _current_branch()
    if len(args) == 1:
        # 'git push origin' — pushes current branch to remote
        return args[0], _current_branch()
    if len(args) >= 2:
        # 'git push origin main' or 'git push origin feature:main'
        remote = args[0]
        refspec = args[1]
        # Handle refspec like 'feature:main'
        branch = refspec.split(":")[-1] if ":" in refspec else refspec
        return remote, branch

    return None, None


def main() -> int:
    try:
        raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if not raw:
            return 0

        data = json.loads(raw)
        cmd = data.get("command", "")
        if not cmd:
            return 0

        # ── git push (any branch) ──────────────────────────────────
        if "git push" in cmd:
            _remote, branch = _get_push_remote_and_branch(cmd)
            print(
                f"BLOCKED: git push requires user approval before "
                f"publishing code externally (target: {branch or 'default'}).",
                file=sys.stderr,
            )
            print(
                "Ask the user: 'Ready to push?' before proceeding.",
                file=sys.stderr,
            )
            return 2

        # ── git merge into main ─────────────────────────────────────
        if "git merge" in cmd:
            current = _current_branch()
            if current in ("main", "master"):
                print(
                    "BLOCKED: Merging into main directly is not allowed.",
                    file=sys.stderr,
                )
                print(
                    "Use the PR workflow instead.",
                    file=sys.stderr,
                )
                return 2

        # ── gh pr create ───────────────────────────────────────────
        if "gh pr create" in cmd:
            print(
                "BLOCKED: Creating a PR requires user approval before "
                "publishing externally.",
                file=sys.stderr,
            )
            print(
                "Ask the user: 'Ready to create the PR?' before proceeding.",
                file=sys.stderr,
            )
            return 2

        # ── gh pr merge without --admin ────────────────────────────
        if "gh pr merge" in cmd and "--admin" not in cmd:
            print(
                "BLOCKED: gh pr merge without --admin is not allowed.",
                file=sys.stderr,
            )
            print(
                "Use: gh pr merge --squash --admin",
                file=sys.stderr,
            )
            return 2

    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open on parse errors

    return 0


if __name__ == "__main__":
    sys.exit(main())
