"""Commit-identity staleness — a stdlib-only leaf so both the MCP guard
(Part A, imports ``fastmcp``) and the dashboard (genesis-server, must NOT import
``fastmcp``) share ONE verdict, provably identical, with no drift.
"""

from __future__ import annotations

from datetime import datetime


def same_commit(a: str | None, b: str | None) -> bool:
    """True if two commit refs denote the same commit, tolerating short/full SHA.

    ``update_history.new_commit`` is a SHORT sha (e.g. ``b08a95c8``); a captured
    spawn identity is the FULL ``rev-parse HEAD``. Neither length is fixed, so
    compare by prefix in both directions rather than equality. Empty/None on
    either side is never a match.
    """
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


def is_stale(
    spawn_commit: str | None,
    spawn_at: str | None,
    deploy_completed_at: str | None,
    deploy_new_commit: str | None,
) -> bool:
    """True iff a process is running code OLDER than the deployed commit.

    Requires BOTH: a commit MISMATCH (``spawn_commit`` differs from the deploy's
    ``new_commit``) AND that the deploy completed AFTER the process started
    (``deploy_completed_at > spawn_at``). The time axis is what distinguishes a
    session that is BEHIND the deploy (stale — restart to catch up) from one that
    is AHEAD of it (a main tree advanced by a manual ``git pull`` past the last
    recorded ``update.sh`` deploy — NOT stale, restarting would not help). Identity
    alone would wrongly flag the ahead case.

    Fail-open: any missing/unparseable input yields ``False`` — never a false
    positive. This is THE staleness verdict; Part A's guard and Part B's dashboard
    badge both call it so they can never disagree.
    """
    if not spawn_commit or not spawn_at or not deploy_completed_at or not deploy_new_commit:
        return False
    if same_commit(spawn_commit, deploy_new_commit):
        return False
    try:
        return datetime.fromisoformat(deploy_completed_at) > datetime.fromisoformat(spawn_at)
    except (ValueError, TypeError):
        return False
