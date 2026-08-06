"""This MCP subprocess's code identity, captured ONCE at process start.

A Claude Code session spawns its FastMCP stdio subprocesses once; they import
genesis tool code at start and never reload. The stale-code guard compares the
commit this process was spawned at against the most recent recorded deploy — so
we must snapshot BOTH the commit and the start time eagerly in ``main()``.

EAGER capture is mandatory (not lazy-on-first-call): a session can start
pre-deploy and not call a guarded tool until after a deploy lands. Capturing
``spawn_at`` lazily on that first call would record a POST-deploy timestamp and
the guard would never recognise the process as stale.

``spawn_commit`` is read from ``env.repo_root()`` (the main tree — MCP servers
always launch from there), so a worktree session still reports main's HEAD, the
same line the deploy advances. On any git failure it is ``None`` → the guard
fails open (never blocks a process whose code identity is unknowable).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from genesis.env import repo_root
from genesis.observability.git_health import _CHEAP_TIMEOUT_S, _run_git

logger = logging.getLogger(__name__)

_spawn_commit: str | None = None
_spawn_at: str | None = None


def capture_spawn_identity() -> None:
    """Record this subprocess's code commit + start time ONCE, at process start.

    Idempotent: subsequent calls are no-ops so a re-entrant bootstrap can't
    overwrite the true (earliest) start time. ``_run_git`` never raises.
    """
    global _spawn_commit, _spawn_at
    if _spawn_at is not None:
        return
    _spawn_at = datetime.now(UTC).isoformat()
    rc, out, _ = _run_git(repo_root(), "rev-parse", "HEAD", timeout=_CHEAP_TIMEOUT_S)
    _spawn_commit = out.strip() if rc == 0 and out.strip() else None

    # Part B: also PERSIST the spawn commit (keyed by GENESIS_SLOT, validated by
    # the claude session pid on read) so the dashboard — a DIFFERENT process —
    # can render a stale-code badge. Fully isolated in its own try/except: a
    # persistence failure must NEVER perturb the in-memory identity the Part A
    # guard depends on. No-op for a non-slotted (headless) launch.
    try:
        slot = os.environ.get("GENESIS_SLOT")
        if slot and _spawn_commit:
            from genesis.observability.mcp_spawn_store import (
                persist_spawn_commit,
                session_pid,
            )

            persist_spawn_commit(slot, session_pid(), _spawn_commit, _spawn_at)
    except Exception:
        logger.debug("spawn-commit persistence failed (non-fatal)", exc_info=True)


def spawn_commit() -> str | None:
    """The full commit SHA this process was spawned at, or None if unknowable."""
    return _spawn_commit


def spawn_at() -> str | None:
    """ISO-8601 UTC timestamp of process start, or None if never captured."""
    return _spawn_at
