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

import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from genesis.db.crud.task_states import list_active

logger = logging.getLogger(__name__)

_MAX_TASKS = 5
_MAX_PLANS = 3
_MAX_CHARS = 2000
_MAX_DESC = 80

# The worktree board, as computed by scripts/worktree_lifecycle.py. This block
# READS that cache rather than classifying worktrees itself, and the reason is
# measured: classification costs ~12s for the 191 linked worktrees on this
# install (2026-09-10), dominated by seven `git rev-parse` calls per worktree in
# the in-progress-operation check. Twelve seconds is not a session-start budget.
# The reaper already does this work daily, so reading its answer keeps ONE source
# of truth and pays a single file read.
_BOARD_CACHE = Path.home() / ".genesis" / "worktree-board.json"
_AT_RISK_SEEN = Path.home() / ".genesis" / "worktree-at-risk-seen.json"
_MAX_NAMED_AT_RISK = 5
# Beyond this the cache describes a tree that has moved on; say so rather than
# reporting stale branch names as if they were current.
_BOARD_STALE_HOURS = 48

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


def _read_board() -> tuple[list[dict], float | None]:
    """The reaper's cached classification as ``(rows, age_hours)``.

    ``([], None)`` means no usable cache — which is a real answer, not an error:
    on a box where the daily timer has never run there is nothing to report and
    guessing would be worse than saying so.
    """
    try:
        raw = json.loads(_BOARD_CACHE.read_text())
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return [], None
    if not isinstance(raw, dict):
        return [], None
    rows = raw.get("worktrees")
    if not isinstance(rows, list):
        return [], None
    age_h: float | None = None
    try:
        gen = datetime.fromisoformat(str(raw.get("generated_at") or ""))
        age_h = (datetime.now(UTC) - gen).total_seconds() / 3600
    except (ValueError, TypeError):
        age_h = None
    return rows, age_h


def _newly_at_risk(current: set[str]) -> set[str]:
    """Which at-risk branches are new since the last session read this.

    The CHANGE is the signal. A static list of two dozen aging branches is the
    "+178 more" failure with a smaller number — identical every session, so it
    stops being read. Naming only the arrivals keeps the line worth looking at.

    Best-effort on both sides: an unreadable state file means everything reads as
    new (noisy once, never wrong), and an unwritable one means the same set is
    reported again next session. Neither is worth failing a session start over.
    """
    try:
        prior = json.loads(_AT_RISK_SEEN.read_text())
        seen = set(prior.get("at_risk", [])) if isinstance(prior, dict) else set()
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        seen = set()
    try:
        _AT_RISK_SEEN.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: concurrent sessions start at the same moment often enough, and a
        # half-written file degrades to "everything is new" — noisy, and noisy in
        # the one line whose value is that it is quiet when nothing changed.
        tmp = _AT_RISK_SEEN.with_name(f"{_AT_RISK_SEEN.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({"at_risk": sorted(current)}))
            tmp.replace(_AT_RISK_SEEN)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
    except OSError:
        logger.debug("in-flight: could not persist at-risk state", exc_info=True)
    return current - seen


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
    """Unlanded work that is AT RISK — a triage, not a roster.

    At risk means: not merged, idle past the first threshold, and not yet reaped.
    That set is bounded by construction — it drains into the archive at the
    second threshold — so unlike a full worktree listing it cannot grow without
    limit. Merged and fresh worktrees are deliberately invisible here: nothing
    about them needs a human, and listing them is what made the old block
    unreadable.

    Returns [] when there is nothing to say, so the section disappears rather
    than reassuring anyone that it ran.
    """
    rows, age_h = _read_board()
    if not rows:
        return []

    if age_h is not None and age_h > _BOARD_STALE_HOURS:
        return [
            f"- board is {age_h / 24:.0f}d stale — branch names below would be "
            f"guesses; run `python3 scripts/worktree_lifecycle.py --report-json`",
        ]

    at_risk = [r for r in rows if isinstance(r, dict) and r.get("state") == "at_risk"]
    if not at_risk:
        return []

    def _label(r: dict) -> str:
        return str(r.get("branch") or "").strip() or Path(str(r.get("path", ""))).name

    labels = {_label(r) for r in at_risk if _label(r)}
    arrivals = sorted(_newly_at_risk(labels))

    lines: list[str] = []
    if arrivals:
        shown = arrivals[:_MAX_NAMED_AT_RISK]
        more = len(arrivals) - len(shown)
        suffix = f" (+{more} more new)" if more else ""
        lines.append(f"- {len(arrivals)} newly at risk: {', '.join(shown)}{suffix}")

    aging = len(labels) - len(arrivals)
    if aging > 0:
        lines.append(f"- {aging} more aging, unmerged and idle")

    due = sum(1 for r in rows if isinstance(r, dict) and r.get("action") == "trash")
    tail = f"- {len(rows)} worktrees tracked · {due} due for archiving"
    if age_h is not None:
        tail += f" · board {age_h:.0f}h old"
    lines.append(tail)
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
        parts.append("\n**Unlanded work at risk:**\n" + "\n".join(worktrees))
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
