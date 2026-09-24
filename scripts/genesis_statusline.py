#!/usr/bin/env python3
"""Genesis status line for Claude Code's native ``statusLine`` setting.

Renders one line of session state that otherwise lives a command away:

    <branch> · ledger:<open rows> · streak:<n>/<cap> · PR#<n>:<review state>

Wire it in user settings (``~/.claude/settings.json``), pointing at the MAIN
checkout — run from a linked worktree the database resolves to that worktree's
absent ``data/`` and the ledger field reads ``—``::

    "statusLine": {"type": "command",
                   "command": "python3 <repo>/scripts/genesis_statusline.py"}

The status-line slot is SINGULAR. To keep another status line you already use,
chain it with ``--then '<its command>'``: that command receives the same stdin
and its output is printed below this line.

Sources — each read through the module that owns it, never re-implemented here:

- branch: ``review_state.get_current_branch``.
- streak: ``review_state.get_review_counters`` — the LOCAL cross-model review
  streak the commit gate's round-2 / round-3 interventions read (branch-scoped,
  legacy-counter aware). It is NOT the GitHub reviewed-heads budget the merge
  and review-request gates also enforce; that needs a GitHub read and is not
  shown here.
- ledger: the canonical ``session_ledger`` table, read-only (WAL-aware URI,
  admission fence honoured). Deliberately NOT the ``charter.md`` mirror: writers
  outside the ledger MCP tools change the DB without refreshing it, and a
  measured mirror disagreed with the DB.
- PR: Claude Code's own stdin payload (``pr.number`` / ``pr.review_state``) —
  no GitHub call from this script.

Advisory by construction: any field it cannot read renders as ``—``, and the
script exits 0 with a line, because Claude Code blanks the status line on a
non-zero exit or empty output. Nothing enforces anything from it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "src", _REPO / "scripts", _REPO / "scripts" / "hooks"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_SEP = " · "
_ABSENT = "—"


def _db_path() -> Path:
    from genesis.env import genesis_db_path

    return genesis_db_path()


def _review_state(cwd: str | None) -> tuple[int, str, int]:
    """(streak, branch, cap) — branch is ``"unknown"`` outside a git checkout."""
    import review_state

    streak, _lifetime = review_state.get_review_counters(cwd)
    return streak, review_state.get_current_branch(cwd=cwd), review_state.ESCALATION_ROUND_CAP


def _open_ledger_rows(session_id: object) -> int | None:
    """Open + in-progress ledger rows for the session, or None when unknown.

    None covers: no session id, a missing/fenced/locked DB, an un-migrated
    install, and a session with no charter row yet (no ledger exists, which is
    not the same as an empty one).
    """
    import sqlite3

    from db_admission_check import database_is_fenced
    from session_heartbeat import ro_uri

    if not isinstance(session_id, str) or not session_id:
        return None
    db = _db_path()
    if not db.exists() or database_is_fenced(db):
        return None
    conn = sqlite3.connect(ro_uri(db), uri=True, timeout=0.5)
    try:
        conn.execute("PRAGMA busy_timeout=300")
        if (
            conn.execute(
                "SELECT 1 FROM session_charters WHERE session_id = ?", (session_id,)
            ).fetchone()
            is None
        ):
            return None
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM session_ledger WHERE session_id = ?"
            " AND status IN ('open','in_progress')",
            (session_id,),
        ).fetchone()
        return int(n)
    finally:
        conn.close()


def _pr_field(data: dict) -> str:
    pr = data.get("pr")
    number = pr.get("number") if isinstance(pr, dict) else None
    if not isinstance(number, int) or isinstance(number, bool):
        return f"PR:{_ABSENT}"
    state = pr.get("review_state")
    return f"PR#{number}" + (f":{state}" if isinstance(state, str) and state else "")


def render(data: dict) -> str:
    cwd = data.get("cwd")
    if not isinstance(cwd, str):
        ws = data.get("workspace")
        cwd = ws.get("current_dir") if isinstance(ws, dict) else None

    branch, streak = _ABSENT, f"streak:{_ABSENT}"
    try:
        n_streak, current, cap = _review_state(cwd)
        # "unknown" means no branch could be resolved — a 0 streak there would
        # be a claim about a checkout that does not exist.
        if current and current != "unknown":
            branch, streak = current, f"streak:{n_streak}/{cap}"
    except Exception:  # noqa: BLE001 — advisory display; a field degrades, the line survives
        pass

    try:
        n = _open_ledger_rows(data.get("session_id"))
    except Exception:  # noqa: BLE001 — same contract
        n = None
    ledger = f"ledger:{_ABSENT if n is None else n}"

    return _SEP.join((branch, ledger, streak, _pr_field(data)))


#: Cap on the chained command. Failure mode it bounds: Claude Code cancels an
#: in-flight status-line command only when the NEXT update arrives, and "slow
#: scripts block the status line from updating until they complete" (CC
#: statusline docs) — so a chained command that hangs in an idle session leaves
#: the line stale indefinitely. MEASURED: the chained Node status line this was
#: built beside runs in ~120ms, so 5s is >40x its normal cost and is reached only
#: by a command that is actually stuck. Owner-chosen value (2026-09-23).
_CHAINED_TIMEOUT_S = 5.0


def _kill_group(proc) -> None:
    """SIGKILL the chained command's whole process group (the shell AND what it
    spawned).

    The group id IS ``proc.pid`` — ``start_new_session`` makes the shell its
    group leader — so no lookup. Two guards, both load-bearing:
    - ``returncode is None``: once the shell has been REAPED its pid (and so its
      group id) may be reused by an unrelated process; killing it then would hit
      a stranger's group. An exited-but-unreaped shell still pins the id
      (MEASURED: getpgid on the zombie returns its own pid), so this is the
      exact line between safe and unsafe.
    - ``pgid > 1``: killpg(1) signals every process this user owns.
    """
    import os
    import signal

    pgid = proc.pid
    if proc.returncode is None and pgid > 1:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


def _disarm_sigterm() -> None:
    """Restore the default SIGTERM once the chained command is finished, so a
    late cancel cannot reach a group id that has since been released."""
    import signal

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _start_chained(cmd: str, raw: str):
    """Start the chained command now so its latency overlaps our render.

    It gets its OWN process group so a timeout can kill everything it spawned,
    not just the shell. That also takes it out of OUR group, which Claude Code
    signals to cancel us — so a SIGTERM handler forwards the kill. Residual: a
    SIGKILL of this script cannot be caught, and would orphan a chained command
    that is itself hung.
    """
    import signal

    proc = subprocess.Popen(  # noqa: S602 — operator-configured command, same trust as statusLine itself
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def _on_term(_signum, _frame):
        _kill_group(proc)
        sys.exit(0)

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _on_term)
    return proc, raw.encode("utf-8", "surrogateescape")


def _finish_chained(started) -> str:
    """The chained command's stdout, or "" when it failed. Never raises.

    Bytes in, bytes out, decoded with replacement: a chained command emitting
    non-UTF-8 must cost its own rows, never blank ours. Past
    ``_CHAINED_TIMEOUT_S`` its process group is killed and reaped, and it
    contributes nothing.
    """
    try:
        proc, payload = started
        try:
            out, _ = proc.communicate(payload, timeout=_CHAINED_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            # NO unbounded drain here. A descendant that left the group (setsid)
            # survives the kill still holding the stdout pipe, so communicate()
            # would wait for IT — MEASURED: a 1s cap held for 20s behind
            # `setsid sleep 20 &`. Drop our end of the pipes and reap the shell
            # with a bounded wait; an escaped descendant is outside anything a
            # process-group kill can reach and is left to finish on its own.
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1)
            return ""
        if proc.returncode != 0 or not out:
            return ""
        text = out.decode("utf-8", "replace")
        return text if text.endswith("\n") else text + "\n"
    except Exception:  # noqa: BLE001 — see docstring
        return ""
    finally:
        _disarm_sigterm()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--then",
        metavar="CMD",
        help="another statusLine command to run with the same stdin; its output "
        "is printed below this line",
    )
    args = parser.parse_args(argv)

    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")

    raw = sys.stdin.read()
    started = None
    if args.then:
        try:
            started = _start_chained(args.then, raw)
        except Exception:  # noqa: BLE001 — a chained command that cannot start costs only its rows
            started = None

    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    try:
        line = render(data)
    except Exception:  # noqa: BLE001 — last resort: an empty line blanks the whole status line
        line = _SEP.join((_ABSENT, f"ledger:{_ABSENT}", f"streak:{_ABSENT}", f"PR:{_ABSENT}"))
    print(line, flush=True)

    if started is not None:
        sys.stdout.write(_finish_chained(started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
