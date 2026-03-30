#!/usr/bin/env python3
"""Review state tracker — manages review markers for enforcement hooks.

Used by:
- review_enforcement_prompt.py (UserPromptSubmit hook)
- review_enforcement_commit.py (PreToolUse hook)
- genesis_stop_hook.py (Stop hook)
- Claude (after /review + code-reviewer agent complete)

The marker file records a hash of ``git diff --cached --stat`` (staged changes
only) at the time review was done.  If staged content changes, the marker
becomes stale and review is required again.  Unstaged working-tree edits
(e.g. from Codex) do not trigger review enforcement.

CLI usage:
    python3 review_state.py status     # prints current review state
    python3 review_state.py mark --review-log <path> --agent-output <path>
    python3 review_state.py diff-hash  # prints current diff hash
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_STATE_FILE = Path.home() / ".genesis" / "review_state.json"
_MAX_EVIDENCE_AGE_SECONDS = 1800  # 30 minutes
_GSTACK_ANALYTICS = Path.home() / ".gstack" / "analytics" / "skill-usage.jsonl"


def get_current_diff_hash(cwd: str | None = None) -> str:
    """SHA-256 of ``git diff --cached --stat`` output (staged changes only).

    Only staged changes trigger review enforcement.  Unstaged changes
    (e.g. from Codex or other tools editing the working tree) are ignored
    so they don't cause false-positive review blocks.

    Args:
        cwd: Working directory for git commands. When None, uses the
             process CWD. Pass a worktree path to check worktree state.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        content = result.stdout.strip()
        if not content:
            return "clean"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"


def has_code_changes(cwd: str | None = None) -> bool:
    """Check if there are any uncommitted code changes."""
    return get_current_diff_hash(cwd=cwd) not in ("clean", "unknown")


def is_review_current() -> bool:
    """Check if the stored review marker matches current diff state."""
    current = get_current_diff_hash()
    if current in ("clean", "unknown"):
        return True  # No changes = no review needed
    if not _STATE_FILE.exists():
        return False
    try:
        state = json.loads(_STATE_FILE.read_text())
        return state.get("diff_hash") == current
    except (json.JSONDecodeError, OSError):
        return False


def has_valid_review_marker() -> bool:
    """Check if a review marker file exists and is not expired.

    Unlike is_review_current(), this does NOT short-circuit on clean staged
    area. Used when the caller knows changes are about to be staged (e.g.
    git add && git commit in the same command).
    """
    if not _STATE_FILE.exists():
        return False
    try:
        state = json.loads(_STATE_FILE.read_text())
        reviewed_at = state.get("reviewed_at", "")
        if not reviewed_at:
            return False
        ts = datetime.fromisoformat(reviewed_at)
        age = (datetime.now(UTC) - ts).total_seconds()
        return age <= _MAX_EVIDENCE_AGE_SECONDS
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def _verify_review_log() -> tuple[bool, str]:
    """Verify gstack /review ran recently (via skill-usage.jsonl)."""
    if not _GSTACK_ANALYTICS.exists():
        return False, "No gstack analytics file found"
    try:
        lines = _GSTACK_ANALYTICS.read_text().strip().splitlines()
        now = time.time()
        for line in reversed(lines[-50:]):  # Check last 50 entries
            try:
                entry = json.loads(line)
                if entry.get("skill") == "review":
                    ts_str = entry.get("ts", "")
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    age = now - ts.timestamp()
                    if age <= _MAX_EVIDENCE_AGE_SECONDS:
                        return True, f"Review ran {int(age)}s ago"
            except (json.JSONDecodeError, ValueError, KeyError):
                continue
        return False, "No recent /review entry in gstack analytics (within 30 min)"
    except OSError as e:
        return False, f"Cannot read analytics: {e}"


def _verify_agent_output(path: str) -> tuple[bool, str]:
    """Verify code-reviewer agent output file exists and is recent."""
    p = Path(path)
    if not p.exists():
        return False, f"Agent output file not found: {path}"
    if p.stat().st_size == 0:
        return False, f"Agent output file is empty: {path}"
    age = time.time() - p.stat().st_mtime
    if age > _MAX_EVIDENCE_AGE_SECONDS:
        return False, f"Agent output is stale ({int(age)}s old, max {_MAX_EVIDENCE_AGE_SECONDS}s)"
    return True, f"Agent output valid ({int(age)}s old, {p.stat().st_size} bytes)"


def mark_reviewed(agent_output_path: str | None = None) -> bool:
    """Write review marker after verifying evidence.

    Returns True if marker was written, False if evidence checks failed.
    """
    # Check 1: gstack /review must have run recently
    review_ok, review_msg = _verify_review_log()
    if not review_ok:
        print(f"REFUSED: {review_msg}", file=sys.stderr)
        print("Run /review first, then try again.", file=sys.stderr)
        return False

    # Check 2: code-reviewer agent output must exist and be recent
    agent_path = agent_output_path or str(Path.home() / ".genesis" / "last_code_review.txt")
    agent_ok, agent_msg = _verify_agent_output(agent_path)
    if not agent_ok:
        print(f"REFUSED: {agent_msg}", file=sys.stderr)
        print("Dispatch the superpowers:code-reviewer agent first.", file=sys.stderr)
        return False

    # Both checks passed — write marker
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "diff_hash": get_current_diff_hash(),
        "reviewed_at": datetime.now(UTC).isoformat(),
        "review_evidence": review_msg,
        "agent_evidence": agent_msg,
    }
    _STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"Review marker written: {state['diff_hash']}")
    return True


def get_current_branch(cwd: str | None = None) -> str:
    """Get current git branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5, cwd=cwd,
        )
        return result.stdout.strip() or "unknown"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: review_state.py [status|mark|diff-hash]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        diff_hash = get_current_diff_hash()
        changes = has_code_changes()
        current = is_review_current()
        print(f"diff_hash: {diff_hash}")
        print(f"has_changes: {changes}")
        print(f"review_current: {current}")
        if _STATE_FILE.exists():
            state = json.loads(_STATE_FILE.read_text())
            print(f"last_reviewed: {state.get('reviewed_at', 'unknown')}")
            print(f"stored_hash: {state.get('diff_hash', 'none')}")

    elif cmd == "diff-hash":
        print(get_current_diff_hash())

    elif cmd == "mark":
        # Parse --agent-output argument
        agent_path = None
        for i, arg in enumerate(sys.argv[2:], 2):
            if arg == "--agent-output" and i + 1 < len(sys.argv):
                agent_path = sys.argv[i + 1]
        if not mark_reviewed(agent_path):
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
