#!/usr/bin/env python3
"""PostToolUse hook: emit one trace span per CC tool call (flat-file).

Fires only for Genesis-DISPATCHED sessions launched with an active trace —
``GENESIS_TRACE_ID`` is set by ``CCInvoker._build_env`` when a span is on the
ContextVar at dispatch. Foreground sessions (not spawned via CCInvoker) never
carry it, so this no-ops for them. This is the INVERSE of session_observer_hook
(which skips dispatched sessions): here we capture what the dispatched agents do.

Appends a JSONL span record to ``~/.genesis/spans/incoming/<key>.jsonl``; the
server-side ingest (``genesis.observability.span_ingest``) drains it into
``otel_spans``. Writing a flat file — NOT the DB — keeps this high-frequency
cross-process hook off the WAL write lock (the file_modification_audit_hook does
write the DB, but only on Write/Edit; a ``.*`` matcher fires far more often).

Budget: <50ms (JSON parse + locked file append). No LLM, no network, no SQLite.
Honors ``GENESIS_SPANS_INCOMING_DIR`` (test override).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import uuid
from pathlib import Path

# Low-signal tools — same exclusions as session_observer_hook (UI/meta only).
_SKIP_TOOLS = frozenset({
    "AskUserQuestion", "TodoWrite", "ListMcpResourcesTool", "Skill",
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "ToolSearch", "EnterPlanMode", "ExitPlanMode", "EnterWorktree",
    "ExitWorktree", "SendMessage", "NotebookEdit",
})

# Max file size before dropping (prevents unbounded growth if ingest is down).
_MAX_FILE_BYTES = 2_000_000


def _incoming_dir() -> Path:
    override = os.environ.get("GENESIS_SPANS_INCOMING_DIR")
    base = override or os.path.expanduser("~/.genesis/spans/incoming")
    return Path(base)


def _key_info(tool_name: str, tool_input: dict) -> dict:
    """Small, secret-free attribute summary by tool type (no file contents)."""
    info: dict = {}
    if tool_name in ("Read", "Write") or tool_name == "Edit":
        info["file_path"] = tool_input.get("file_path", "")
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        info["command"] = cmd[:200] if cmd else ""
    elif tool_name in ("Glob", "Grep"):
        info["pattern"] = tool_input.get("pattern", "")
    elif tool_name == "WebFetch":
        info["url"] = tool_input.get("url", "")
    elif tool_name == "WebSearch":
        info["query"] = tool_input.get("query", "")
    elif tool_name == "Agent":
        info["description"] = tool_input.get("description", "")
        info["subagent_type"] = tool_input.get("subagent_type", "")
    else:
        # MCP/unknown tools — record scalar keys only, truncated.
        for key in list(tool_input.keys())[:5]:
            val = tool_input[key]
            if isinstance(val, str):
                info[key] = val[:120]
            elif isinstance(val, (int, float, bool)):
                info[key] = val
    return info


def _safe_key(candidate: str, fallback: str) -> str:
    if (
        not candidate
        or len(candidate) > 200  # guard against pathological filenames
        or "/" in candidate
        or "\\" in candidate
        or ".." in candidate
    ):
        return fallback
    return candidate


def _process(data: dict) -> None:
    trace_id = os.environ.get("GENESIS_TRACE_ID")
    if not trace_id:
        return  # not a traced dispatched session → no-op

    tool_name = data.get("tool_name", "")
    if not tool_name or tool_name in _SKIP_TOOLS:
        return

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    session_id = os.environ.get("GENESIS_SESSION_ID") or data.get("session_id") or ""
    parent_span_id = os.environ.get("GENESIS_PARENT_SPAN_ID")
    now_us = int(time.time() * 1_000_000)

    record = {
        "span_id": uuid.uuid4().hex,
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
        "name": f"cc.tool.{tool_name}",
        "kind": "tool",
        "status": "ok",
        # Point-in-time: PostToolUse has no start; duration unknown.
        "start_unix_us": now_us,
        "end_unix_us": now_us,
        "duration_us": None,
        "session_id": session_id or None,
        "attributes": _key_info(tool_name, tool_input),
    }

    key = _safe_key(session_id, trace_id)
    incoming = _incoming_dir()
    incoming.mkdir(parents=True, exist_ok=True)
    target = incoming / f"{key}.jsonl"
    try:
        if target.exists() and target.stat().st_size > _MAX_FILE_BYTES:
            return
    except OSError:
        pass

    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(target, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(line)
        fcntl.flock(fh, fcntl.LOCK_UN)


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        _process(json.loads(raw))
    except Exception:
        # Hooks must never crash or block a tool call.
        return


if __name__ == "__main__":
    main()
