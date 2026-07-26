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
from hook_input import field, read_payload, strip_quoted  # noqa: E402

# Pattern to detect git commit commands (but not git commit --amend, etc. — those
# are also commits and should be blocked)
_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")
_ADD_PATTERN = re.compile(r"\bgit\s+add\b")
# --no-verify / -n (standalone or bundled, e.g. -nm) — the flag that skips ALL
# pre-commit hooks. Matched on the quote-stripped command. The bundled-short
# alternative can rarely over-match `-mn` (message literally "n"); that fails
# safe (blocks a degenerate command) and is preferable to missing `-an`/`-sn`.
_NO_VERIFY_PATTERN = re.compile(r"--no-verify\b|(?:^|\s)-[a-zA-Z]*n[a-zA-Z]*(?=[\s=]|$)")
# Override sigil: a trailing shell comment acknowledging accepted findings.
# The `#` must open a real comment (preceded by whitespace/start) so a token
# jammed into an unquoted message word (`-m x#review-override`) is NOT treated
# as an override. Trailing text after the sigil is allowed (it's commented out).
_OVERRIDE_PATTERN = re.compile(r"#\s*review-override\b")
_OVERRIDE_TRAILING = re.compile(r"(?:^|\s)#\s*review-override\b")


def _override_status(command: str) -> str:
    """Classify a ``# review-override`` token in a commit command.

    Returns:
      ``"valid"``    — a genuine shell comment (outside quotes, ``#`` opening a
                       real comment): an intentional override.
      ``"in_quote"`` — the token appears only inside a quoted region (e.g. the
                       ``-m`` message) or jammed into a word, where it would be
                       committed into public history and does NOT override.
      ``"none"``     — no override token present.
    """
    if _OVERRIDE_TRAILING.search(strip_quoted(command)):
        return "valid"
    if _OVERRIDE_PATTERN.search(command):
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

    # Rule 0: Block --no-verify / -n — it bypasses ALL pre-commit hooks
    # (review enforcement AND the native secrets / large-file / direct-to-main
    # guards). No legitimate reason to skip them. Match both the long flag and
    # the short -n form (standalone or bundled, e.g. -nm) on the quote-stripped
    # command so a "--no-verify" mentioned inside the commit message doesn't
    # false-block. This runs BEFORE the override check, so it can never be
    # bypassed by '# review-override'.
    if _NO_VERIFY_PATTERN.search(strip_quoted(command)):
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
    stages_in_same_command = bool(_ADD_PATTERN.search(command))
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
        override = _override_status(command)
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
