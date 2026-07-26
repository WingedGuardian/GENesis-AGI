#!/usr/bin/env python3
"""PreToolUse hook (Bash): block commits without review.

Two enforcement rules:
1. Block ALL commits directly to main — always require a branch.
2. Block commits on branches if review marker is not current.

Reads the CC hook payload from stdin (via hook_input).

Exit codes:
  0 = allow (tool proceeds)
  2 = deny (tool blocked, message on stderr)
"""

from __future__ import annotations

import os
import re
import sys

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
from hook_input import field, read_payload  # noqa: E402
from shell_parse import analyze, commit_skips_hooks, git_subcommand  # noqa: E402

# Cheap early-out: a raw command that never mentions "git commit" is not a
# commit. Actual detection (through wrappers, bash -c, etc.) uses shell_parse.
_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")


def _commit_override(command: str, segs: list) -> str:
    """Classify the ``# review-override`` approval for the git-commit segment(s).

    Returns:
      ``"valid"``    — every executed ``git commit`` segment carries a genuine
                       trailing ``# review-override`` shell comment (bound to
                       that segment by shell_parse): an intentional override.
      ``"in_quote"`` — the token appears in the command (e.g. inside the ``-m``
                       message or a heredoc body) but not as a clean trailing
                       comment on the commit, where it would leak into public
                       history and does NOT override.
      ``"none"``     — no override token present.
    """
    commit_segs = [s for s in segs if git_subcommand(s.argv) == "commit"]
    if commit_segs and all(s.override for s in commit_segs):
        return "valid"
    if re.search(r"#\s*review-override\b", command):
        return "in_quote"
    return "none"


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
    command = field(read_payload(), "command")
    if not _COMMIT_PATTERN.search(command):
        sys.exit(0)  # Not a commit, allow

    # Parse the command into the segments it actually executes (through
    # wrappers, bash -c, command substitutions). Reused for Rule 0, the
    # add-chain detection, and the override binding.
    segs = analyze(command)

    # Rule 0: Block --no-verify / -n on ANY executed commit segment — it
    # bypasses ALL pre-commit hooks (review enforcement AND the native secrets /
    # large-file / direct-to-main guards). shell_parse parses real argv, so a
    # flag mentioned inside the commit message doesn't false-block and a bundled
    # / operator-glued / bash -c-nested form isn't missed. Runs BEFORE the
    # override check, so it can never be bypassed by '# review-override'.
    if any(commit_skips_hooks(s.argv) for s in segs):
        _deny(
            "BLOCKED: --no-verify / -n bypasses review enforcement AND the "
            "native pre-commit guards (secrets, large files, direct-to-main). "
            "Remove it and establish a review first via /review."
        )
        return

    # Detect worktree: extract working directory from 'cd /path && git commit'
    cwd = _extract_working_dir(command)

    # Import review_state from same directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)

    try:
        from review_state import (
            get_current_branch,
            has_code_changes,
            has_valid_review_marker,
            is_review_current,
        )
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
    stages_in_same_command = any(git_subcommand(s.argv) == "add" for s in segs)
    if stages_in_same_command:
        # Can't check diff hash (staging hasn't happened yet) — require the
        # marker file to exist and not be expired.
        rule2_blocks = not has_valid_review_marker()
    else:
        rule2_blocks = has_code_changes(cwd=cwd) and not is_review_current()

    if rule2_blocks:
        # A trailing '# review-override' comment (outside quotes) acknowledges
        # accepted findings and bypasses ONLY this review gate — never Rule 0
        # (--no-verify) or Rule 1 (main-branch), which are checked above.
        override = _commit_override(command, segs)
        if override == "valid":
            print(
                "NOTE: review-override honored — commit review gate bypassed. "
                "Findings acknowledged by session.",
                file=sys.stderr,
            )
            sys.exit(0)
        if override == "in_quote":
            _deny(
                "BLOCKED: '# review-override' is not a clean trailing shell "
                "comment — it sits inside quotes (e.g. the commit message) or is "
                "followed by more command, so it does NOT override and could be "
                "committed into public history. Put it at the very END, outside "
                "any quotes:\n"
                '  git commit -m "your message"  # review-override'
            )
            return
        _deny(
            "BLOCKED: Code changes exist without review. "
            "Run /review and dispatch the superpowers:code-reviewer agent first, "
            "then run: python3 scripts/review_state.py mark --agent-output ~/.genesis/last_code_review.txt\n"
            "If findings are intentionally accepted, append a trailing shell "
            "comment (outside any quotes): '  # review-override'"
        )
        return

    # All checks passed — allow
    sys.exit(0)


def _deny(message: str) -> None:
    """Output denial message and block the tool via exit code 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
