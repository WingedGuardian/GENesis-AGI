"""Commit identity: deploy-based blocking and separate recorded-revision advice."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

SAME_RECORDED_REVISION = "same_recorded_revision"
DIFFERENT_RECORDED_REVISION = "different_recorded_revision"
UNKNOWN = "unknown"
_HEAD_TIMEOUT_S = 1.0


def _full_commit(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value):
        return None
    return value.lower() if value.strip("0") else None


def recorded_revision(launch: object, current: object) -> str:
    """Compare complete recorded IDs; never assert imported-source equivalence.

    Unlike the existing deploy predicate, abbreviated or mixed-format IDs cannot
    establish this relation. No history ordering or successful deploy is needed.
    """
    launch = _full_commit(launch)
    current = _full_commit(current)
    if launch is None or current is None or len(launch) != len(current):
        return UNKNOWN
    return SAME_RECORDED_REVISION if launch == current else DIFFERENT_RECORDED_REVISION


def read_runtime_head() -> str | None:
    """Bounded local read from the same root authority used by spawn capture.

    Read the ref itself, without object peeling/history or a network operation.
    An observed SHA is not a measurement of dirty, imported, or candidate bytes.
    A root mismatch, unavailable Git, or timeout leaves the advice unknown.
    """
    from genesis.env import repo_root
    from genesis.session_awareness.zero_drop_git import scrubbed_git_env

    try:
        root = repo_root().resolve()
        result = subprocess.run(
            ["git", "--no-optional-locks", "--no-replace-objects", "-C", str(root),
             "rev-parse", "--show-toplevel", "--verify", "HEAD"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=_HEAD_TIMEOUT_S, env=scrubbed_git_env(),
        )
        lines = result.stdout.rstrip("\n").split("\n")
        if result.returncode != 0 or len(lines) != 2 or Path(lines[0]).resolve() != root:
            return None
        return _full_commit(lines[1])
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return None


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
    positive. The blocking guard and retained dashboard compatibility fields use
    this verdict; the recorded-revision advisory is independent.
    """
    if not spawn_commit or not spawn_at or not deploy_completed_at or not deploy_new_commit:
        return False
    if same_commit(spawn_commit, deploy_new_commit):
        return False
    try:
        return datetime.fromisoformat(deploy_completed_at) > datetime.fromisoformat(spawn_at)
    except (ValueError, TypeError):
        return False
