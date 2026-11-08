#!/usr/bin/env python3
"""Nonblocking edit advice and first claims through the shared claim library."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    try:
        import worktree_claim as claim
        from hook_input import field, read_payload, session_id

        payload = read_payload()
        event = payload.get("hook_event_name")
        tool = payload.get("tool_name")
        if event not in {"PreToolUse", "PostToolUse"} or tool not in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
            return 0
        if claim.effective_mode() == "off":
            return 0
        raw = field(payload, "notebook_path" if tool == "NotebookEdit" else "file_path")
        if not raw:
            return 0
        path = Path(raw).expanduser()
        if not path.is_absolute():
            cwd = payload.get("cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                return 0
            path = Path(cwd) / path
        root = claim.worktree_root_for(path)
        if root is None:
            return 0
        lock = claim.read_lock(root)
        actor = claim.session_pid_from_ancestry()
        if actor is None:
            return 0
        if event == "PreToolUse":
            if lock is None or lock.foreign or lock.payload is None:
                return 0
            owner, start = lock.payload.get("pid"), lock.payload.get("start")
            # The reaper's liveness predicate intentionally protects UNKNOWN.
            # An advisory requires positive identity, not that release fallback.
            if owner == actor or start is None or claim.proc_starttime(owner) != start:
                return 0
            fields = claim._proc_fields(owner)
            if not fields or fields[0] in {"Z", "X", "x"}:
                return 0
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"Worktree ownership advisory: this worktree is claimed by live session pid {owner}. Coordinate with that session before editing its work. This advisory does not block the edit.",
            }}))
        elif lock is None:
            response = payload.get("tool_response", {})
            if not isinstance(response, dict) or response.get("is_error") or response.get("error"):
                return 0
            identity = claim.build_payload(pid=actor, sid=session_id(payload, default=""))
            if identity is not None:
                claim.lock_worktree(root, identity)
    except Exception:  # noqa: BLE001 — advisory/claim unavailability never blocks an edit
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
