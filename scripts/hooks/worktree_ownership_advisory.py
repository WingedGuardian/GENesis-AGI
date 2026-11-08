#!/usr/bin/env python3
"""Nonblocking edit advice and first claims through the shared claim library."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    try:
        import worktree_claim as claim
        from hook_input import field, is_safe_session_id, read_payload, session_id
        from hook_output import print_json_bounded

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
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return 0
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(cwd) / path
        root = claim.worktree_root_for(path)
        if root is None:
            return 0
        # Geometry is generic; only this hook scopes ownership to its shipped
        # repository. Payload cwd is context, never the repository authority.
        common_dirs = []
        for directory in (Path(__file__).resolve().parents[2], Path(cwd), root):
            result = claim._git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if result is None or result.returncode or not result.stdout:
                return 0
            common_dirs.append(Path(result.stdout.removesuffix("\n")).resolve())
        if len(set(common_dirs)) != 1:
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
            sid = lock.payload.get("sid")
            identity = f"{sid} (pid {owner})" if is_safe_session_id(sid) else f"pid {owner}"
            print_json_bounded({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"Worktree ownership advisory: this worktree is claimed by live session {identity}. Coordinate with that session before editing its work. This advisory does not block the edit.",
            }}, text_keys=("hookSpecificOutput.additionalContext",))
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
