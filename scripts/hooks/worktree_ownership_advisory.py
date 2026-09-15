#!/usr/bin/env python3
"""Hook: claim a worktree on first edit, and warn before editing someone else's.

Two modes, both non-blocking, both on the Edit/Write family:

    --claim    (PostToolUse)  after a successful write into an unclaimed
                              worktree, record that this session holds it
    --advise   (PreToolUse)   before writing into a worktree another LIVE
                              session holds, say so IN THE MODEL'S CONTEXT and
                              allow it
    --release  (SessionEnd)   drop every claim this session still holds, so a
                              claimer never ships without a releaser

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
sessions that nobody intended to stop. So: never a non-zero exit, every time.

WHY JSON AND NOT STDERR -- the correction this file exists in its current shape
for. An earlier version printed the warning to stderr and exited 0, and I
"verified" it by running the hook as a subprocess and reading stderr back. That
proves EMISSION. It does not prove DELIVERY, and delivery is the entire point of
a component whose only job is to tell somebody something.

It was INERT. Per this repo's own measured contract
(docs/reference/cc-compatibility.md:1093-1099) only SessionStart,
UserPromptSubmit and UserPromptExpansion put a hook's stdout in front of the
model; PreToolUse and PostToolUse reach it ONLY through JSON
``hookSpecificOutput.additionalContext``. A PreToolUse hook that prints advice
and exits 0 writes to the debug log and nothing else. No session would ever have
seen this warning.

Two details that are easy to get wrong and are load-bearing here: a TOP-LEVEL
``additionalContext`` key is silently discarded -- it must nest under
``hookSpecificOutput`` (scripts/hooks/web_tools_gate.py records the same trap) --
and the payload goes through ``print_json_bounded`` so that an oversized advisory
loses its PROSE rather than its envelope.

FAIL-OPEN THROUGHOUT. A hook must never crash a session or deny a tool call over
its own bug, so every path returns 0 and unexpected exceptions are swallowed by
``run_guard``. The cost of a missed advisory is one warning; the cost of a
crashed hook is the session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import worktree_claim as wc  # noqa: E402  (sibling import, stdlib-only module)
from hook_input import field, read_payload, session_id  # noqa: E402
from hook_output import print_json_bounded  # noqa: E402

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
    note = (
        f"NOTE: {root.name} is claimed by {who} (pid {holder}), which is still running. "
        f"Editing {os.path.basename(target)} here may collide with its uncommitted work. "
        "Not blocked — check with that session, or work in your own worktree."
    )
    # `hookSpecificOutput`, NOT a top-level `additionalContext`: the latter is
    # silently discarded by Claude Code, which is the same failure mode as the
    # stderr version this replaced — output that exists and never arrives.
    # Routed through `print_json_bounded` so an oversized payload loses the
    # PROSE and keeps the envelope; without that a long worktree name could
    # cost the whole advisory rather than shorten it.
    print_json_bounded(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": note,
            }
        },
        text_keys=("hookSpecificOutput.additionalContext",),
    )
    return 0


def _release(payload: dict) -> int:
    """Release every claim this session holds. Wired to SessionEnd.

    A CLAIMER WITHOUT A RELEASER IS A LEAK, and shipping one without the other
    was a real gap in this stack rather than a theoretical one: the redesign that
    removed the daily sweep removed the only thing that released claims, so every
    first edit took a lock nothing ever dropped. The reaper then skips a locked
    worktree permanently, and git refuses to delete one without a force flag, so
    edited worktrees accumulate locks forever.

    MEASURED on this install: a claim taken by one session sat for three days
    after that session exited and had to be released by hand. That is the leak
    observed, not predicted.

    SessionEnd is the fast path and covers the ordinary case — a session that
    ends normally drops what it holds. It is NOT sufficient alone and is not
    claimed to be: a SIGKILLed or crashed session never runs it. The backstop is
    the reaper releasing a claim whose process is gone, which ``is_releasable``
    already decides and which lands separately. Until then a crashed session's
    claim is recoverable exactly the way that three-day-old one was, and the lock
    reason says so in its own first sentence.

    Enumerates once via porcelain and releases ONLY locks whose recorded pid is
    this session's. Another session's claim, and any FOREIGN lock, are untouched.
    """
    mine = wc.session_pid_from_ancestry()
    if mine is None:
        return 0

    # `-z` because a worktree path may legally contain a NEWLINE on Unix, and
    # porcelain puts that newline INSIDE the `worktree <path>` value. Splitting on
    # lines then yields a truncated, nonexistent root, so the real worktree is
    # never visited and its claim survives the session that took it — the precise
    # leak this function exists to close, reintroduced by the parser. With `-z`,
    # records are NUL-terminated and the path is unambiguous.
    #
    # The env scrub is the same trap as in the ownership check: an exported
    # GIT_DIR / GIT_COMMON_DIR would enumerate ANOTHER repository's worktrees,
    # and we would then read — and try to release — locks that are not ours.
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(Path(__file__).resolve().parent.parent.parent),
        env=wc._git_env(),
    )
    if result.returncode != 0:
        return 0

    released = 0
    for field_ in result.stdout.split("\0"):
        if not field_.startswith("worktree "):
            continue
        root = Path(field_[len("worktree ") :])
        lock = wc.read_lock(root)
        if lock is None or lock.foreign or lock.rule != wc.RULE_CLAIM:
            continue
        if lock.payload.get("pid") != mine:
            continue  # someone else's claim is not ours to drop
        if wc.unlock_worktree(root):
            released += 1
    if released:
        print(
            f"released {released} worktree claim(s) held by this session",
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
    payload = read_payload()
    # RELEASE RUNS BEFORE THE MODE GATE, and the ordering is the fix.
    #
    # Turning the feature off must not strand the claims it already took. With
    # the gate first, a session that had claimed worktrees and then saw `enabled`
    # or `mode` flipped to off exited without unlocking ANY of them — and a
    # locked worktree is skipped unconditionally by the reaper, so the claims
    # would sit there permanently with the feature that created them switched
    # off and no longer able to clean up after itself.
    #
    # It also contradicted this repo's own shipped promise:
    # config/worktree_ownership.yaml says `off` does not release claims already
    # taken because "a claim releases when its process exits regardless of this
    # setting, so they drain on their own". That was documentation describing
    # behaviour the code did not have; releasing first is what makes it true.
    #
    # Only RELEASE is exempt. Claiming and advising are the feature, and `off`
    # correctly stops them.
    if "--release" in sys.argv:
        return _release(payload)
    if wc.effective_mode() == "off":
        return 0
    if "--claim" in sys.argv:
        return _claim(payload)
    if "--advise" in sys.argv:
        return _advise(payload)
    return 0


if __name__ == "__main__":
    # FAIL-OPEN, deliberately NOT `run_guard`. That helper converts any unhandled
    # exception into exit 2, and on PreToolUse exit 2 DENIES the tool call — so a
    # bug in this advisory would block an edit it has no business blocking. Its
    # own docstring says as much: "never for advisory or convenience guards,
    # which must stay fail-open so a bug never blocks legit work." Wiring the
    # fail-closed runner here contradicted that instruction directly.
    #
    # The exception is still printed, because "never hide broken things" applies
    # to an advisory too — it just must not cost the user their edit. Matches the
    # shape edit_verify_advisory.py uses for the same reason.
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — an advisory never blocks
        print(
            f"ADVISORY ERROR (worktree_ownership_advisory): failing OPEN — "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(0)
