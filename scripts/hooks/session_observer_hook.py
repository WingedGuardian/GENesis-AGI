#!/usr/bin/env python3
"""PostToolUse hook: capture tool activity for session note-taking.

Appends a structured observation to a per-session JSONL file so the
async processor (in the awareness loop) can batch-extract and store
as memories.  The current session benefits from its own activity via
proactive recall of the stored notes.

Budget: <50ms (JSON parse + file append).  No LLM, no network, no SQLite.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

# Skip in dispatched CC sessions (reflections, surplus, inbox evaluations)
if os.environ.get("GENESIS_CC_SESSION") == "1":
    sys.exit(0)

# Tools that produce low-signal observations — not worth capturing
_SKIP_TOOLS = frozenset({
    "AskUserQuestion",
    "TodoWrite",
    "ListMcpResourcesTool",
    "Skill",
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "ToolSearch",
    "EnterPlanMode",
    "ExitPlanMode",
    "EnterWorktree",
    "ExitWorktree",
    "SendMessage",
    "NotebookEdit",
})

# Max chars to capture from tool output
_OUTPUT_CAP = 2000
# Max chars to capture from tool input
_INPUT_CAP = 1500
# Max JSONL file size before dropping observations (prevents unbounded growth)
_MAX_FILE_BYTES = 500_000  # ~500KB, roughly 200-300 observations


def _extract_key_info(tool_name: str, tool_input: dict) -> dict:
    """Extract the most useful information from tool input by tool type."""
    info: dict = {}
    if tool_name in ("Read", "Write"):
        info["file_path"] = tool_input.get("file_path", "")
    elif tool_name == "Edit":
        info["file_path"] = tool_input.get("file_path", "")
        # Capture what changed for richer LLM context
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if old:
            info["old_string"] = old[:150]
        if new:
            info["new_string"] = new[:150]
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        info["command"] = cmd[:500] if cmd else ""
    elif tool_name in ("Glob", "Grep"):
        info["pattern"] = tool_input.get("pattern", "")
        info["path"] = tool_input.get("path", "")
    elif tool_name == "WebFetch":
        info["url"] = tool_input.get("url", "")
    elif tool_name == "WebSearch":
        info["query"] = tool_input.get("query", "")
    elif tool_name == "Agent":
        info["description"] = tool_input.get("description", "")
        info["subagent_type"] = tool_input.get("subagent_type", "")
    else:
        # MCP tools or unknown — capture first few keys
        for key in list(tool_input.keys())[:5]:
            val = tool_input[key]
            if isinstance(val, str):
                info[key] = val[:200]
            elif isinstance(val, (int, float, bool)):
                info[key] = val
    return info


def _truncate_output(output_raw: str) -> str:
    """Truncate tool output, keeping head for context."""
    if not output_raw or len(output_raw) <= _OUTPUT_CAP:
        return output_raw or ""
    return output_raw[:_OUTPUT_CAP] + f"\n... [truncated, {len(output_raw)} total chars]"


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
        _process(data)
    except Exception:
        # Hooks must never crash or block
        return


def _process(data: dict) -> None:
    tool_name = data.get("tool_name", "")
    session_id = data.get("session_id", "")

    if not tool_name or not session_id:
        return

    # Skip low-signal tools
    if tool_name in _SKIP_TOOLS:
        return

    # Validate session_id as safe path component
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        return

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    # Get tool output from environment (CC PostToolUse contract)
    output_raw = os.environ.get("CLAUDE_TOOL_USE_RESULT", "")

    observation = {
        "ts": time.time(),
        "session_id": session_id,
        "tool_name": tool_name,
        "key_info": _extract_key_info(tool_name, tool_input),
        "output_summary": _truncate_output(output_raw),
    }

    # Append to per-session JSONL file
    session_dir = Path(os.path.expanduser("~/.genesis/sessions")) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    obs_file = session_dir / "tool_observations.jsonl"

    # Size gate: drop observations if file is too large (processor may be down)
    try:
        if obs_file.exists() and obs_file.stat().st_size > _MAX_FILE_BYTES:
            return
    except OSError:
        pass

    # Locked append: flock prevents interleaved writes when CC fires
    # parallel tool calls (JSONL lines can exceed PIPE_BUF of 4096 bytes)
    line = json.dumps(observation, separators=(",", ":")) + "\n"
    with open(obs_file, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
