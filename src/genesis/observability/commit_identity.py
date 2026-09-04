"""Commit-identity verdicts — a stdlib-only leaf so every consumer shares one
implementation without importing ``fastmcp`` (the dashboard runs inside
genesis-server, which must not).

There are deliberately TWO verdicts, and they answer different questions. Do
not "unify" them — the divergence is the design.

``differs_from_head`` — AWARENESS. Does this process's code differ from the
tree's CURRENT HEAD? Used by the advisory surfaces (the per-prompt hook nudge
and the dashboard code-differs badge). It observes HEAD directly, so it needs
no producer and cannot be bypassed.

``is_stale`` — AUTHORIZATION. Did a RECORDED deploy land after this process
started? Used by the MCP middleware guard, which BLOCKS a guarded tool. It is
deliberately the conservative verdict: it only fires on a deploy written to
``update_history`` by ``scripts/update.sh``.

Why they differ, measured on a live install (2026-09-03). Two figures, and they
answer DIFFERENT questions — quoting either as "the" number is wrong:

* **Record coverage — 5 rows for 46 HEAD movements over 30 days (10.9%).** How
  often a code change is recorded at all.
* **Verdict recall — 4/52 (7.7%) against 52/52 for ``differs_from_head``.** A
  replay over the same history asking, per movement, whether a session spawned
  just before it would actually be TOLD. It is lower than coverage, and the gap is
  the time axis: a recorded deploy only helps a session that started before it.
  (52 vs 46 because the replay counts every HEAD-moving reflog entry, including
  the checkouts the coverage figure excludes.)

The dominant deploy path is the pull-plus-reinstall-plus-restart sequence that
``scripts/lib/live_system_guard.sh`` (see its refusal message) calls "the
sanctioned path" for a code-only change, and it writes no row.

The practical consequence, stated precisely: after any pull-based deploy, a
newly spawned session is invisible to ``is_stale`` **until the next
``update.sh`` run** — not forever, but for as long as that path keeps being
used, which on this install has meant days at a stretch while the tree moved
dozens of commits.

That blindness is a defect in an ADVISORY surface (a warning that never fires)
and merely conservative in a BLOCKING one (a gate that declines to block).
Repointing the blocking guard at ``differs_from_head`` would widen a live block
from ~never-firing to firing on most long-lived sessions — a policy change, not
a bug fix. It is therefore left on ``is_stale`` until that decision is taken
deliberately.
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


def differs_from_head(spawn_commit: str | None, head_commit: str | None) -> bool:
    """True iff a process's code differs from the tree's CURRENT HEAD.

    The AWARENESS verdict (see the module docstring). No time axis: ``is_stale``
    needs one only to tell "behind the last recorded deploy" from "ahead of it",
    and nothing can be ahead of live HEAD. Both sides resolve to the same line —
    a spawn identity is captured from ``env.repo_root()`` (the main tree, even
    for a worktree session; see ``mcp_spawn_identity``) and the caller reads HEAD
    from that same tree.

    IT IS AN INEQUALITY, AND THE NAME NOW SAYS SO. This was ``is_behind``, and
    every consumer that read the name rather than the docstring inherited a
    direction it does not establish: the dashboard badge said "running older
    code", which is false after a reset, a force-move, or a checkout of an
    earlier ref — cases where the process holds code the tree no longer has.
    Two shas cannot answer "which way"; only the history can, and this leaf
    deliberately does no IO. ``genesis_urgent_alerts._deploy_span`` answers it
    where the git data is, by reading ``spawn...head`` with ``--left-right`` and
    reporting BOTH sides rather than one count.

    Fail-open: a missing value on either side yields ``False``.
    """
    return bool(spawn_commit and head_commit and not same_commit(spawn_commit, head_commit))


def is_stale(
    spawn_commit: str | None,
    spawn_at: str | None,
    deploy_completed_at: str | None,
    deploy_new_commit: str | None,
) -> bool:
    """True iff a process is running code OLDER than the last RECORDED deploy.

    The AUTHORIZATION verdict (see the module docstring) — the conservative one,
    used where the consequence is a BLOCK. Requires BOTH: a commit MISMATCH
    (``spawn_commit`` differs from the deploy's ``new_commit``) AND that the
    deploy completed AFTER the process started (``deploy_completed_at >
    spawn_at``). The time axis is what distinguishes a session BEHIND the last
    recorded deploy from one AHEAD of it (a main tree advanced by a manual
    ``git pull`` past the last recorded ``update.sh`` deploy — restarting would
    not help against THIS record). Identity alone would wrongly flag the ahead
    case.

    Note what that means in practice, and why ``differs_from_head`` exists: a
    session ahead of the last recorded deploy can still be far behind live
    HEAD. This verdict is silent there by design.

    Fail-open: any missing/unparseable input yields ``False`` — never a false
    positive.
    """
    if not spawn_commit or not spawn_at or not deploy_completed_at or not deploy_new_commit:
        return False
    if same_commit(spawn_commit, deploy_new_commit):
        return False
    try:
        return datetime.fromisoformat(deploy_completed_at) > datetime.fromisoformat(spawn_at)
    except (ValueError, TypeError):
        return False
