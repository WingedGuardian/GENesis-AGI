#!/usr/bin/env python3
"""Hook: claim a worktree on first edit, and warn before editing someone else's.

Two modes, both non-blocking, both on the Edit/Write family:

    --claim    (PostToolUse)  after a successful write into an unclaimed
                              worktree, record that this session holds it
    --advise   (PreToolUse)   before writing into a worktree another LIVE
                              session holds, say so on stderr and allow it

WHY CLAIM ON EDIT RATHER THAN ONLY ON CREATION. There are ~200 worktrees already
and sessions mostly ADOPT one rather than create it, so a claim taken only at
``git worktree add`` would be absent in the common case -- and an advisory that
can never fire is worse than none, because it reads as coverage. The first write
into a worktree is the moment a session actually starts owning it.

WHY ADVISORY AND NOT A BLOCK. The house rule is that advisory is the default and
escalating to a block needs a specific, measured reason. This does not have one:
editing another session's worktree is a mistake an agent corrects the moment it
is told, and the damage it prevents (a lost uncommitted change) is already
covered by the lock keeping the reaper away. A block would also stop background
sessions that nobody intended to stop. So: stderr, exit 0, every time.

FAIL-OPEN THROUGHOUT. A hook must never crash a session or deny a tool call over
its own bug, so every path returns 0 and unexpected exceptions are swallowed by
``run_guard``. The cost of a missed advisory is one warning; the cost of a
crashed hook is the session.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import worktree_claim as wc  # noqa: E402  (sibling import, stdlib-only module)
from hook_input import field, read_payload, run_guard, session_id  # noqa: E402

# The tool-input keys that name a file across the Edit/Write family. NotebookEdit
# uses `notebook_path`; the rest use `file_path`. A key not listed here simply
# yields no path and the hook does nothing, which is the correct fail-open.
_PATH_FIELDS = ("file_path", "notebook_path")


def _target_path(payload: dict) -> str:
    for name in _PATH_FIELDS:
        value = field(payload, name)
        if value:
            return value
    return ""


def _advise(payload: dict) -> int:
    """Warn when the target sits in a worktree another live session claims."""
    target = _target_path(payload)
    if not target:
        return 0
    root = wc.worktree_root_for(target)
    if root is None:
        return 0  # the main checkout, or not a worktree at all

    lock = wc.read_lock(root)
    if lock is None or lock.foreign or lock.rule != wc.RULE_CLAIM:
        return 0

    holder = lock.payload.get("pid")
    if not wc.pid_is_live_session(holder, lock.payload.get("start")):
        return 0  # a stale claim; the sweep will release it

    mine = wc.session_pid_from_ancestry()
    if mine is not None and mine == holder:
        return 0  # our own claim

    sid = lock.payload.get("sid")
    who = f"session {sid}" if sid else "another session"
    print(
        f"NOTE: {root.name} is claimed by {who} (pid {holder}), which is still running. "
        f"Editing {os.path.basename(target)} here may collide with its uncommitted work. "
        "Not blocked — check with that session, or work in your own worktree.",
        file=sys.stderr,
    )
    return 0


def _claim(payload: dict) -> int:
    """Take an unclaimed worktree for this session after a successful write."""
    target = _target_path(payload)
    if not target:
        return 0
    root = wc.worktree_root_for(target)
    if root is None:
        return 0
    if wc.read_lock(root) is not None:
        return 0  # already owned -- by us, by another session, or by a foreign tool

    sid = session_id(payload, default="")
    payload_json = wc.build_payload(wc.RULE_CLAIM, sid=sid or None)
    if payload_json is None:
        # No resolvable session process means no release condition, and a lock
        # without one would pin this worktree against the reaper indefinitely.
        return 0
    wc.lock_worktree(root, payload_json)
    return 0


def main() -> int:
    if wc.effective_mode() == "off":
        return 0
    payload = read_payload()
    if "--claim" in sys.argv:
        return _claim(payload)
    if "--advise" in sys.argv:
        return _advise(payload)
    return 0


if __name__ == "__main__":
    run_guard(main, "worktree_ownership_advisory")
