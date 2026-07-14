#!/usr/bin/env python3
"""Duplicate-session guard: one live executor per CC conversation transcript.

Incident (2026-07-13): a dropped SSH connection left a `claude` process
executing its turn headless (sshd dead-client detection was disabled, so the
half-open TCP kept the pty alive ~2h), while the user's re-SSH + resume
spawned a SECOND executor over the SAME conversation. Both mutated the same
worktree. Reproduced on CC 2.1.201: `claude --resume` of a running session
fires no native lock (idle or mid-turn) and both processes append to one
transcript jsonl.

Two modes, both registered in .claude/settings.json:

--register (UserPromptSubmit + PostToolUse, every tool call):
    Maintain ~/.genesis/session-owners/<transcript-key>.json — who (pid,
    starttime) currently executes this transcript. If a DIFFERENT live claude
    pid already owns it, write <transcript-key>.conflict naming both
    executors (deterministic order) instead of silently stealing ownership.
    Always exits 0.

guard mode, the default (PreToolUse on Write|Edit|NotebookEdit|Bash):
    Fast path: no conflict file for this transcript -> allow (one stat).
    With a live conflict, NEWEST WINS: the older executor's repo-mutating
    tools are denied (exit 2) with recovery instructions; the newer executor
    passes. Read-only tools and MCP tools are not matched at all (v1 scope).

Deny requires positive evidence of ALL of: parseable conflict file, both
pids alive with matching starttimes, own claude ancestor identified and
present in the file, self strictly older by (starttime, pid) total order.
ANY other state — parse failure, walk failure, missing transcript_path,
dead peer, third executor not in the file — fails OPEN (allow): a guard bug
must never brick a legitimate single session. Escape hatches: touch
<transcript-key>.override (checked every call) or launch with
GENESIS_ALLOW_DUAL_SESSION=1.

Stdlib-only (no genesis.* imports — hook latency budget). The awareness loop
(_check_duplicate_cc_executor) independently pages Telegram on live conflict
files and GCs stale ones; see src/genesis/awareness/loop.py.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proc_ident  # noqa: E402

OWNERS_DIR = Path.home() / ".genesis" / "session-owners"

ALLOW = "allow"
DENY = "deny"
# Shared between decide() and _guard()'s self-heal branch — a reword that
# drifts the two apart would silently disable the hook-side conflict cleanup.
REASON_STALE = "conflict stale (fewer than two live executors)"


def _read_json(path: Path) -> dict | None:
    """Parse a small JSON file; None on ANY failure (missing, torn, garbage)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Temp-file + rename in the same dir (reaper _save_state precedent):
    concurrent writers are last-write-wins, readers never see a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _executor_entry(pid: int, starttime: int, session_id: str) -> dict:
    return {"pid": pid, "starttime": starttime, "session_id": session_id}


def _self_identity() -> tuple[int, int] | None:
    """(pid, starttime) of this hook's claude ancestor, or None."""
    pid = proc_ident.find_claude_ancestor(os.getppid())
    if pid is None:
        return None
    starttime = proc_ident.read_starttime(pid)
    if starttime is None:
        return None
    return pid, starttime


def decide(
    conflict: dict | None,
    my_pid: int | None,
    my_starttime: int | None,
    alive: callable[[int, int], bool] | None = None,
) -> tuple[str, str]:
    """Pure newest-wins decision. Returns (action, reason).

    DENY only on positive evidence; every degraded state allows. Total order
    for "older" is (starttime, pid) so a starttime tie (scripted spawns can
    land in the same jiffy) still picks exactly one loser.
    """
    if alive is None:  # late-bound so tests (and callers) can substitute it
        alive = proc_ident.is_alive
    if not isinstance(conflict, dict):
        return ALLOW, "no parseable conflict"
    if my_pid is None or my_starttime is None:
        return ALLOW, "own claude ancestor not identifiable"

    raw = conflict.get("executors")
    if not isinstance(raw, list) or len(raw) < 2:
        return ALLOW, "conflict file malformed (executors)"
    executors: list[tuple[int, int]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return ALLOW, "conflict file malformed (entry)"
        pid, starttime = entry.get("pid"), entry.get("starttime")
        if not isinstance(pid, int) or not isinstance(starttime, int):
            return ALLOW, "conflict file malformed (types)"
        executors.append((pid, starttime))

    live = [(pid, st) for pid, st in executors if alive(pid, st)]
    if len(live) < 2:
        return ALLOW, REASON_STALE

    me = (my_pid, my_starttime)
    if me not in live:
        # A third executor this conflict doesn't describe — undefined for the
        # two-pid schema; fail open (the registry will re-detect it).
        return ALLOW, "self not among recorded executors"

    newest = max(live, key=lambda e: (e[1], e[0]))
    if me == newest:
        return ALLOW, "self is the newest executor"
    return DENY, f"newer executor pid {newest[0]} owns this conversation"


def _register(payload: dict) -> int:
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        # No key -> do not touch the registry at all (never fall back to a
        # constant key: that would collide unrelated sessions into one file
        # and manufacture phantom conflicts).
        return 0
    ident = _self_identity()
    if ident is None:
        return 0
    my_pid, my_starttime = ident
    session_id = str(payload.get("session_id", "") or "")

    key = proc_ident.transcript_key(transcript_path)
    owner_path = OWNERS_DIR / f"{key}.json"
    conflict_path = OWNERS_DIR / f"{key}.conflict"
    now = time.time()

    me = _executor_entry(my_pid, my_starttime, session_id)
    owner = _read_json(owner_path)
    owner_pid = owner.get("pid") if owner else None
    owner_starttime = owner.get("starttime") if owner else None
    owner_is_valid = isinstance(owner_pid, int) and isinstance(owner_starttime, int)

    if (
        not owner_is_valid
        or (owner_pid, owner_starttime) == (my_pid, my_starttime)
        or not proc_ident.is_alive(owner_pid, owner_starttime)
    ):
        # Unowned, owned by self, or owned by a dead process -> claim it.
        _write_json_atomic(
            owner_path,
            {
                "transcript_path": transcript_path,
                "updated_at": now,
                **me,
            },
        )
        # A conflict that no longer has two live executors is resolved.
        stale = _read_json(conflict_path)
        if stale is not None or conflict_path.exists():
            action, _ = decide(stale, my_pid, my_starttime)
            if action == ALLOW:
                _unlink_quiet(conflict_path)
        return 0

    # A DIFFERENT live claude executes this transcript: record the conflict
    # (deterministic executor order -> racing writers produce identical bytes)
    # and leave the owner file alone. The PreToolUse guard + awareness loop
    # take it from here.
    other = _executor_entry(owner_pid, owner_starttime, str(owner.get("session_id", "") or ""))
    executors = sorted([me, other], key=lambda e: (e["starttime"], e["pid"]))
    _write_json_atomic(
        conflict_path,
        {
            "transcript_path": transcript_path,
            "detected_at": now,
            "executors": executors,
        },
    )
    return 0


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _guard(payload: dict) -> int:
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return 0
    key = proc_ident.transcript_key(transcript_path)
    conflict_path = OWNERS_DIR / f"{key}.conflict"
    if not conflict_path.exists():  # fast path: single stat
        return 0

    override_path = OWNERS_DIR / f"{key}.override"
    if override_path.exists() or os.environ.get("GENESIS_ALLOW_DUAL_SESSION") == "1":
        return 0

    ident = _self_identity()
    my_pid, my_starttime = ident if ident else (None, None)
    action, reason = decide(_read_json(conflict_path), my_pid, my_starttime)

    if action == ALLOW:
        # Self-heal: a conflict without two live executors is stale. (The
        # override file can't exist here — it short-circuits above; the
        # awareness-loop GC owns override cleanup.)
        if reason == REASON_STALE:
            _unlink_quiet(conflict_path)
        return 0

    print(
        f"BLOCKED: duplicate session executor — {reason}.\n"
        f"Two live `claude` processes are executing this SAME conversation "
        f"(transcript {transcript_path}); this instance (pid {my_pid}) is the "
        f"OLDER one, and newest wins. This usually means a dropped SSH left "
        f"this process orphaned while the user resumed elsewhere.\n"
        f"This instance should STOP working: do not retry writes; end the "
        f"turn.\n"
        f"User override (if this concurrency is intentional): "
        f"touch {override_path}\n"
        f"(or relaunch with GENESIS_ALLOW_DUAL_SESSION=1 — env is read at "
        f"claude launch, not mid-session.)",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        if "--register" in sys.argv:
            return _register(payload)
        return _guard(payload)
    except Exception as exc:  # noqa: BLE001 — fail-open, never brick a session
        print(f"duplicate_session_guard: internal error (failing open): {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
