"""Identity columns so the session roster can join heartbeat rows to processes.

Nine sources of session identity exist on an install and none of them joins to
another, because the one edge that would connect them — which OS process a
cc_session_id belongs to — was never recorded. The heartbeat hooks run inside
the session (unsandboxed, with the process ancestry and cwd in hand), so the
heartbeat row is the one place that edge can be written cheaply. Everything a
roster then needs about liveness is OBSERVED at read time from /proc and the
socket dir — deliberately never stored, because stored liveness is stale by
construction (the cc_sessions table's status column is the cautionary tale).

  - ``pid`` — the session's ``claude`` process id, found by walking the hook's
    parent chain to the first ``comm == "claude"``. A fresh walk always yields
    a value, so a resumed session (same cc_session_id, new process) self-heals
    on its next throttled write.
  - ``pid_started_at`` — that process's start time, written and updated as a
    PAIR with ``pid``: it exists solely so a reader can reject a recycled pid
    at observe time instead of calling a stranger's process "alive".
  - ``cwd`` — the session's working directory, from the hook payload.
  - ``git_branch`` — the branch of that cwd; "" means "known: not on a branch
    / not a repo" and NULL means "resolution failed", per the table's
    three-valued contract.
  - ``slot`` — the launcher's GENESIS_SLOT label. Advisory, for display
    surfaces (the dashboard join renders it in the next batch —
    GROUNDWORK(session-roster-pr2)); joins trust the pid, never an
    inherited env label.

All NULLABLE, no defaults, no backfill — rows written before this migration
simply render as identity-unknown, which is the truth. Additive and idempotent
in ``up()``; ``down()`` is destructive by design (dev/test affordance, not a
production rollback path). ALTERs are PRAGMA/duplicate-guarded. Mirrored in
the base ``create_all_tables`` DDL and ``_migrate_add_columns``
(schema_both_build_paths). The runner owns the transaction — no commit here.
"""

from __future__ import annotations

import aiosqlite

_TABLE = "session_heartbeats"

_COLUMNS = (
    ("pid", "INTEGER"),
    ("pid_started_at", "TEXT"),
    ("cwd", "TEXT"),
    ("git_branch", "TEXT"),
    ("slot", "TEXT"),
)


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608 — literal, ours
    return {row[1] for row in await cursor.fetchall()}


async def up(db: aiosqlite.Connection) -> None:
    if not await _table_exists(db, _TABLE):
        return  # fresh DB — create_all_tables already carries the columns
    have = await _columns(db, _TABLE)
    for column, ctype in _COLUMNS:
        if column not in have:
            await db.execute(
                f"ALTER TABLE {_TABLE} ADD COLUMN {column} {ctype}"  # noqa: S608
            )


async def down(db: aiosqlite.Connection) -> None:
    if not await _table_exists(db, _TABLE):
        return
    have = await _columns(db, _TABLE)
    for column, _ in _COLUMNS:
        if column in have:
            await db.execute(
                f"ALTER TABLE {_TABLE} DROP COLUMN {column}"  # noqa: S608
            )
