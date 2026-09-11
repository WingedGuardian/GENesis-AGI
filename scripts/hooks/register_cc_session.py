#!/usr/bin/env python3
"""SessionStart: register this interactive terminal session in cc_sessions.

The missing creation event (measured 2026-09-04): terminal CC sessions had
NO row until a 2-hourly extraction poll adopted them — as 'completed' — so
live sessions read as finished, young ones didn't exist at all, and the
dashboard's fg/bg counts were fiction. This hook writes an honest
``active`` row (terminal convention: ``id == cc_session_id``) with the
claude process's pid — the death-evidence key the reaper's fast path uses
— the moment the session starts. Registration is fail-open by
construction: on ANY miss it exits 0 silently and the adoption backstop
still catches the session on its next pass.

Interactive-only: Genesis's own headless invocations (``-p``/``--print``
one-turn judges, CCInvoker dispatches) manage their own rows or run from
non-project cwds; the PPID walk below finds the nearest ``claude``
ancestor and skips when its cmdline is headless, so those never get a
duplicate row racing their managed one.

Contract: NEVER prints (SessionStart stdout is injected into the session
context — byte-identical silence on every path), never blocks, never
creates the DB file. The pre-migration table guard lives in the crud
helper.
"""

from __future__ import annotations

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_input import read_payload, session_id  # noqa: E402

_PROC = "/proc"


def _claude_ancestor(start_pid: int) -> tuple[int, list[str]] | None:
    """(pid, argv) of the nearest ``claude`` ancestor, or None.

    /proc-based PPID walk (same evidence plane as session_activity_touch.sh):
    comm identifies the process, cmdline is world-readable under the systemd
    sandbox where environ is not. Bounded hop count so a weird process tree
    can never loop the hook.
    """
    pid = start_pid
    for _ in range(20):
        try:
            with open(f"{_PROC}/{pid}/comm") as f:
                comm = f.read().strip()
        except OSError:
            return None
        if comm == "claude":
            try:
                with open(f"{_PROC}/{pid}/cmdline", "rb") as f:
                    argv = f.read().decode(errors="replace").split("\0")
            except OSError:
                argv = []
            return pid, argv
        try:
            with open(f"{_PROC}/{pid}/stat") as f:
                after = f.read().rsplit(")", 1)[1].split()
            pid = int(after[1])  # ppid = field 4 → index 1 after comm
        except (OSError, IndexError, ValueError):
            return None
        if pid <= 1:
            return None
    return None


def main() -> None:
    payload = read_payload()
    sid = session_id(payload, default="")
    if not sid:
        return

    found = _claude_ancestor(os.getppid())
    if found is None:
        return
    pid, argv = found
    if "-p" in argv or "--print" in argv:
        return  # headless invocation — not a terminal session

    try:
        from genesis.env import genesis_db_path

        db_path = genesis_db_path()
        if not db_path.exists():
            return  # pre-bootstrap: never create the DB from a hook
    except Exception:
        return

    model = None
    try:
        from session_heartbeat import cached_model

        model = cached_model(sid)
    except Exception:
        model = None

    try:
        from genesis.db.crud.cc_sessions import register_terminal_session_sync

        register_terminal_session_sync(str(db_path), sid, pid=pid, model=model)
    except Exception:
        return  # adoption backstop covers a failed registration


if __name__ == "__main__":
    # silent exit 0 on every path — stdout is session context
    with contextlib.suppress(Exception):
        main()
