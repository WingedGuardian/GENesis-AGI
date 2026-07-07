"""In-flight working-state block for session-start context.

Builds a terse, mechanical snapshot of what is currently *in flight* — active
autonomy tasks, live git worktrees, and recently-touched plan files — for a
fresh foreground session to silently pick up. This is **situational awareness
for the session itself, not a status report for the user**: the block leads
with an explicit anti-dump directive because it folds directly under Essential
Knowledge (which carries no such framing), and a fresh session must never open
by reciting it.

Computed fresh at session start (worktrees/plans change far faster than the L1
Essential-Knowledge regeneration cadence, so a generator-side write would be
stale-on-arrival). Every section is independently guarded; an empty snapshot
returns "" so the caller emits nothing.

Consumed by ``scripts/genesis_session_context.py`` (foreground branch only).
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from genesis.db.crud.task_states import list_active

logger = logging.getLogger(__name__)

_MAX_TASKS = 5
_MAX_WORKTREES = 8
_MAX_PLANS = 3
_MAX_CHARS = 2000
_MAX_DESC = 80

# Copied (not imported) from outreach.morning_report._relative_age: that module's
# top-level imports pull in the content/routing chain (ContentDrafter, etc.),
# which is the wrong cost + coupling for a latency-critical session-start hook.
# This is a trivial stdlib-only pure helper; the duplication is deliberate.


def _relative_age(iso_ts: str) -> str:
    """Convert an ISO timestamp to a human-readable relative age string."""
    if not iso_ts:
        return "unknown age"
    try:
        ts = datetime.fromisoformat(iso_ts)
        delta = datetime.now(UTC) - ts
        total_s = delta.total_seconds()
        if total_s < 0:
            return "just now"
        if total_s < 3600:
            return f"{int(total_s / 60)}m ago"
        if total_s < 86400:
            return f"{total_s / 3600:.0f}h ago"
        return f"{total_s / 86400:.0f}d ago"
    except (ValueError, TypeError):
        return "unknown age"


def _safe_mtime(path: str) -> float:
    """File mtime, or 0.0 if the path is missing/unreadable."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _list_worktrees(repo_root: Path) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into structured data.

    Returns list of dicts with keys: path, head, branch (branch absent for a
    detached HEAD). Excludes the main worktree (the first porcelain entry).

    Copied from ``scripts/worktree_lifecycle.py:_list_worktrees`` (scripts/ is
    not an importable package) with the subprocess timeout tightened from 10s
    to 2s for the session-start hook budget. Follow-up: extract a shared util
    so this logic is not maintained in two places.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=2,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    worktrees: list[dict] = []
    current: dict = {}
    is_first = True

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current and "path" in current and not is_first:
                worktrees.append(current)
            current = {"path": line[len("worktree "):]}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "":
            if current and "path" in current and not is_first:
                worktrees.append(current)
            is_first = False
            current = {}

    if current and "path" in current and not is_first:
        worktrees.append(current)

    return worktrees


async def _task_lines(db) -> list[str]:
    """Active (non-terminal) autonomy tasks, newest first, capped."""
    rows = await list_active(db)
    lines: list[str] = []
    for r in rows[:_MAX_TASKS]:
        tid = (r["task_id"] or "")[:8]
        phase = r["current_phase"] or "?"
        desc = (r["description"] or "").strip().replace("\n", " ")
        if len(desc) > _MAX_DESC:
            desc = desc[: _MAX_DESC - 3] + "..."
        age = _relative_age(r["updated_at"] or "")
        lines.append(f"- `{tid}` {phase} · {desc} ({age})")
    extra = len(rows) - _MAX_TASKS
    if extra > 0:
        lines.append(f"- …(+{extra} more)")
    return lines


def _worktree_lines(repo_root: Path) -> list[str]:
    """Live git worktrees (main tree excluded), most-recently-touched first."""
    wts = _list_worktrees(repo_root)
    wts.sort(key=lambda w: _safe_mtime(w.get("path", "")), reverse=True)
    lines: list[str] = []
    for w in wts[:_MAX_WORKTREES]:
        branch = w.get("branch") or Path(w.get("path", "")).name or "detached"
        head = (w.get("head") or "")[:8]
        lines.append(f"- {branch} @ {head}")
    extra = len(wts) - _MAX_WORKTREES
    if extra > 0:
        lines.append(f"- …(+{extra} more)")
    return lines


def _plan_lines(plans_dir: Path) -> list[str]:
    """Recently-modified plan files, newest first, capped."""
    if not plans_dir.exists():
        return []
    files = [p for p in plans_dir.glob("*.md") if p.is_file()]
    files.sort(key=lambda p: _safe_mtime(str(p)), reverse=True)
    lines: list[str] = []
    for p in files[:_MAX_PLANS]:
        age = _relative_age(datetime.fromtimestamp(_safe_mtime(str(p)), UTC).isoformat())
        lines.append(f"- {p.name} ({age})")
    return lines


async def build_inflight_block(db, *, repo_root: Path, plans_dir: Path) -> str:
    """Assemble the in-flight working-state block, or "" when nothing is in flight.

    Each section is independently guarded (mirrors the morning-report assembler
    discipline): one section failing never suppresses the others. The returned
    string includes its own ``### In-flight state`` heading and anti-dump
    directive, and carries NO leading ``---`` divider — the caller folds it
    directly under Essential Knowledge.
    """
    try:
        tasks = await _task_lines(db)
    except Exception:
        logger.debug("in-flight: active-tasks section failed", exc_info=True)
        tasks = []
    try:
        worktrees = _worktree_lines(repo_root)
    except Exception:
        logger.debug("in-flight: worktrees section failed", exc_info=True)
        worktrees = []
    try:
        plans = _plan_lines(plans_dir)
    except Exception:
        logger.debug("in-flight: plans section failed", exc_info=True)
        plans = []

    if not (tasks or worktrees or plans):
        return ""

    parts = [
        "### In-flight state (for your recollection, not a report)",
        "(you already have this context; the user knows what they're working "
        "on. Reference only if relevant to what they raise; never open a "
        "session by summarizing it.)",
    ]
    if tasks:
        parts.append("\n**Active autonomy tasks:**\n" + "\n".join(tasks))
    if worktrees:
        parts.append("\n**Live worktrees:**\n" + "\n".join(worktrees))
    if plans:
        parts.append("\n**Recent plans:**\n" + "\n".join(plans))

    block = "\n".join(parts)
    if len(block) > _MAX_CHARS:
        cut = block[:_MAX_CHARS]
        nl = cut.rfind("\n")
        if nl > 0:
            cut = cut[:nl]
        block = cut.rstrip() + "\n…(truncated)"
    return block
