#!/usr/bin/env python3
"""PostToolUse hook for ExitPlanMode — auto-bookmark plan sessions.

When a plan is approved (ExitPlanMode fires), this hook:
1. Writes a pending bookmark file for the MCP server to consume
2. Outputs the architecture review recommendation as additionalContext

The MCP server picks up the pending file on the next tool call and
creates the bookmark programmatically — no LLM dependency.

Reads hook input from stdin as JSON:
  {"tool_name": "ExitPlanMode", "tool_input": {...}, "tool_output": {...}}

Output format (CC PostToolUse hook contract):
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "..."
  }
}
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

# The architecture review recommendation
_ARCH_REVIEW = (
    "RECOMMENDED: You have exited plan mode. Before implementation, run an "
    "architecture review proportional to the plan scope. For small plans "
    "(1-2 files, wiring changes): dispatch a single code-architect agent to "
    "check dependencies, edge cases, and DRY violations. For medium plans "
    "(3-10 files, new components): run a focused CEO premise challenge + eng "
    "architecture review. For large plans (10+ files, new systems): run the "
    "full /autoplan pipeline (CEO \u2192 design \u2192 eng review). Always surface "
    "findings and update the plan before starting implementation. The goal is "
    "catching real issues, not ceremony."
)

_WORKTREE_REMINDER = (
    "MANDATORY: Before writing any code, create a git worktree for isolation. "
    "Run: git worktree add .claude/worktrees/<scope>-<desc> -b <scope>/<desc> "
    "then work inside the worktree directory. Commit on the branch, merge to "
    "main when done. NEVER commit directly to main. This is enforced by "
    "project convention — skipping it risks cross-session contamination."
)

_CONFIDENCE_REMINDER = (
    "MANDATORY: Before starting implementation, state explicit confidence "
    "percentages for each part of the plan with rationale. Anything below 90% "
    "needs investigation to raise it first. Separate root-cause confidence from "
    "fix confidence when they differ. State what information would move each "
    "item to 100%."
)

_DUE_DILIGENCE_REMINDER = (
    "MANDATORY: Before starting implementation, verify your plan against actual "
    "code. For each file you plan to modify: READ IT FIRST. Confirm the "
    "functions/classes you plan to change actually exist and work the way you "
    "think. Check for recent changes (git log) that might conflict. If your "
    "plan references a table, query its schema. If it references a config, "
    "read the file. 'I assume' is not due diligence — 'I verified' is."
)

_GENESIS_DIR = Path.home() / ".genesis"
_PENDING_FILE = _GENESIS_DIR / "plan_bookmark_pending.json"


def _is_in_worktree() -> bool:
    """Check if the current working directory is inside a git worktree."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return False
        # Check if it's a worktree (not the main working tree)
        result2 = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=2,
        )
        result3 = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=2,
        )
        # In a worktree, --git-dir != --git-common-dir
        return result2.stdout.strip() != result3.stdout.strip()
    except Exception:
        return False


def _extract_plan_info(hook_input: dict) -> tuple[str, str]:
    """Extract plan file path and title.

    Tries hook input first, falls back to most recently modified file
    in ~/.claude/plans/.

    Returns (plan_path, title). Both may be empty if not available.
    """
    tool_input = hook_input.get("tool_input", {})
    tool_output = hook_input.get("tool_output", {})

    plan_path = ""
    title = ""

    # Try to find plan path in hook input/output
    for source_str in (str(tool_output), str(tool_input)):
        if ".claude/plans/" in source_str:
            match = re.search(r"(/[^\s\"']+\.claude/plans/[^\s\"']+\.md)", source_str)
            if match:
                plan_path = match.group(1)
                break

    # Fallback: find most recently modified plan file
    if not plan_path:
        plans_dir = Path.home() / ".claude" / "plans"
        if plans_dir.is_dir():
            try:
                candidates = sorted(
                    plans_dir.glob("*.md"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if candidates:
                    plan_path = str(candidates[0])
            except OSError:
                pass

    # Read the plan title from the first heading
    if plan_path:
        try:
            path = Path(plan_path)
            if path.exists():
                for line in path.read_text().splitlines()[:10]:
                    line = line.strip()
                    if line.startswith("#") and not line.startswith("<!--"):
                        title = line.lstrip("#").strip()
                        break
        except OSError:
            pass

    return plan_path, title


def _guess_session_id() -> str:
    """Best-effort session ID from most recently modified sessions directory."""
    sessions_dir = _GENESIS_DIR / "sessions"
    if not sessions_dir.exists():
        return ""

    # Skip Genesis background sessions (env var check)
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return ""

    try:
        # Find the most recently modified session directory
        candidates = sorted(
            (d for d in sessions_dir.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0].name
    except OSError:
        pass

    return ""


def main() -> int:
    """Read PostToolUse hook input, write pending bookmark, output arch review."""
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    plan_path, title = _extract_plan_info(hook_input)
    session_id_hint = _guess_session_id()

    # Write pending bookmark file for MCP server to consume
    _GENESIS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        pending_data = {
            "plan_path": plan_path,
            "title": title,
            "session_id_hint": session_id_hint,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _PENDING_FILE.write_text(json.dumps(pending_data))
    except OSError as exc:
        print(f"plan_bookmark_hook: failed to write pending file: {exc}", file=sys.stderr)

    # Build implementation checklist
    reminders = [_ARCH_REVIEW, _CONFIDENCE_REMINDER, _DUE_DILIGENCE_REMINDER]
    if not _is_in_worktree():
        reminders.append(_WORKTREE_REMINDER)

    # Output all reminders
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(reminders),
        }
    }

    json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
