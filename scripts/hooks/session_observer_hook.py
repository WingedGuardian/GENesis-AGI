#!/usr/bin/env python3
"""PostToolUse hook: capture tool activity for session note-taking.

Appends a structured observation to a per-session JSONL file so the
async processor (in the awareness loop) can batch-extract and store
as memories.  The current session benefits from its own activity via
proactive recall of the stored notes.

Budget: <50ms (JSON parse + file append).  No LLM, no network, and no SQLite
on the common path -- the throttled cross-session liveness refresh below opens
the database at most once per 60s per session, and its throttle check costs one
stat (measured: median 0.044ms, p95 0.09ms over 2000 calls).
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

# Self-locate so secret_scrub resolves whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_scrub import (  # noqa: E402
    command_touches_secret,
    is_secret_path,
    scrub,
    scrub_info,
)

# Tools that produce low-signal observations — not worth capturing
_SKIP_TOOLS = frozenset(
    {
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
    }
)

# Max chars to capture from tool output
_OUTPUT_CAP = 2000
# Max chars to capture from tool input
_INPUT_CAP = 1500
# Max JSONL file size before dropping observations (prevents unbounded growth)
_MAX_FILE_BYTES = 500_000  # ~500KB, roughly 200-300 observations


def _extract_key_info(tool_name: str, tool_input: dict) -> dict:
    """Extract the most useful information from tool input by tool type."""
    info: dict = {}
    if _is_credential_tool(tool_name):
        # reference_store args ARE the credential — never capture them.
        return {"note": "[redacted: credential-store tool]"}
    if tool_name in ("Read", "Write"):
        info["file_path"] = tool_input.get("file_path", "")
    elif tool_name == "Edit":
        fp = tool_input.get("file_path", "")
        info["file_path"] = fp
        # Capture what changed for richer LLM context — but the before/after
        # bodies of a secret-bearing file ARE secrets, so never persist them.
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if is_secret_path(fp):
            if old or new:
                info["edit"] = "[redacted: secret-bearing file]"
        else:
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
    # Redact inline secret shapes from every captured string value.
    return scrub_info(info)


def _truncate_output(output_raw: str) -> str:
    """Truncate tool output, keeping head for context."""
    if not output_raw or len(output_raw) <= _OUTPUT_CAP:
        return output_raw or ""
    return output_raw[:_OUTPUT_CAP] + f"\n... [truncated, {len(output_raw)} total chars]"


def _is_credential_tool(tool_name: str) -> bool:
    """Reference-store MCP tools carry credentials by design — their args and
    results should not be captured into telemetry at all."""
    return tool_name.lower().endswith(("reference_store", "reference_lookup", "reference_export"))


def _summarize_output(tool_name: str, tool_input: dict, output_raw: str) -> str:
    """Capture tool output for note-taking without persisting secret bodies.

    Reading/editing/searching a secret-bearing path, a Bash command that reads
    one, or a reference-store tool puts raw secrets in the result — skip
    capturing it. Whatever IS captured is still scrubbed for inline secret
    shapes (a token echoed in output, an ``export API_KEY=…``), since this text
    flows on into memory extraction and proactive recall."""
    if not output_raw:
        return ""
    ti = tool_input if isinstance(tool_input, dict) else {}
    path = ti.get("file_path", "") or ti.get("path", "")
    cmd = ti.get("command", "")
    if tool_name in ("Read", "Edit", "Write", "Grep", "Glob") and is_secret_path(path):
        return "[skipped: secret-bearing path]"
    if tool_name == "Bash" and command_touches_secret(cmd):
        return "[skipped: command reads a secret-bearing file]"
    if _is_credential_tool(tool_name):
        return "[skipped: credential-store tool]"
    return scrub(_truncate_output(output_raw))


def main() -> None:
    # Bound BEFORE the try. An earlier version of this function bound `data`
    # inside it and read it in the `finally`, which crashed the hook with an
    # UnboundLocalError on empty stdin and on malformed JSON -- the exact paths
    # the `except Exception: return` exists to swallow -- because an exception
    # raised in a `finally` propagates PAST the except above it. This hook is
    # registered on the ".*" PostToolUse matcher, so that shipped a traceback to
    # the user on every tool call in every session.
    data: dict = {}
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            data = parsed
        _process(data)
    except Exception:
        # Hooks must never crash or block
        return
    finally:
        # Cross-session liveness, deliberately OUTSIDE _process and in its own
        # guard. Outside, because _process returns early for _SKIP_TOOLS and a
        # session that spends five minutes in a skipped tool must not read as
        # dead to its peers. In its own guard, because a refresh failure must
        # never cost the observation append above.
        _maybe_refresh_heartbeat(data.get("session_id", ""))


def _maybe_refresh_heartbeat(session_id: str) -> None:
    """Bump this session's heartbeat ``updated_at``, at most once per window.

    WHY: ``get_active_sync`` hides any row older than 10 minutes, and the only
    other writer is UserPromptSubmit -- so a session working heads-down for
    twenty minutes DISAPPEARS from every peer's awareness while it is busiest.

    ``updated_at`` ONLY. Passing no other field is a pure liveness touch because
    the upsert's conflict clause COALESCEs every content column; that property is
    what makes this safe, and it is pinned by
    tests/test_db/test_session_heartbeats_upsert.py.

    Everything expensive sits behind ``throttle_ok`` -- including the crud import
    (order of 100 ms; measured 102/168/218 across samples), which is therefore
    paid at most once per 60s per session rather than on every tool call. Note
    that laziness INSIDE this function is only half the story: each hook run is a
    fresh process, so `session_heartbeat`'s own module-scope imports are paid
    unconditionally too, which is why `sqlite3` and `urllib.request` are deferred
    inside it (58.0 ms -> 12.7 ms marginal).
    """
    if not session_id:
        return
    try:
        from session_heartbeat import throttle_ok

        if not throttle_ok(session_id):
            return
        from genesis.db.crud.session_heartbeats import upsert_sync
        from genesis.env import genesis_db_path

        db_path = genesis_db_path()
        if not Path(db_path).exists():
            return
        upsert_sync(str(db_path), cc_session_id=session_id)
    except Exception:
        return  # never block the session for an awareness nicety


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

    # Tool result travels in the stdin payload (CC PostToolUse contract); the
    # old CLAUDE_TOOL_USE_RESULT env var is no longer set.
    _resp = data.get("tool_response")
    output_raw = json.dumps(_resp) if _resp else ""

    observation = {
        "ts": time.time(),
        "session_id": session_id,
        "tool_name": tool_name,
        "key_info": _extract_key_info(tool_name, tool_input),
        "output_summary": _summarize_output(tool_name, tool_input, output_raw),
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
