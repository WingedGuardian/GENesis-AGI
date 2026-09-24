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
compose them IN THE SETTINGS COMMAND, giving each the same stdin::

    sh -c 'in=$(cat); printf %s "$in" | python3 <repo>/scripts/genesis_statusline.py;
           printf %s "$in" | <your existing status-line command>'

Composition deliberately lives there rather than in this script: running
another program from here means owning its timeouts, its process group and its
cancellation, and every one of those proved to be its own source of defects.

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

import contextlib
import json
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


def main(argv: list[str] | None = None) -> int:
    """Read CC's JSON payload from stdin, print one status line, exit 0.

    ``argv`` is accepted for call-compatibility and ignored: the script takes
    no options.
    """
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")

    try:
        data = json.loads(sys.stdin.read())
    except (ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    try:
        line = render(data)
    except Exception:  # noqa: BLE001 — last resort: an empty line blanks the whole status line
        line = _SEP.join((_ABSENT, f"ledger:{_ABSENT}", f"streak:{_ABSENT}", f"PR:{_ABSENT}"))
    print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
