"""Pre-execution observation — staleness and context-drift checks.

Deterministic checks run before REVIEWING to detect whether the
codebase or task context has drifted since the plan was written.
No LLM calls — git commands + in-memory task comparison only.

Three checks:
1. Plan age: how old is the task since submission?
2. Git activity: how many commits landed since task creation?
3. Task overlap: have other tasks completed since this one was created?
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (tunable module constants)
# ---------------------------------------------------------------------------

STALE_WARN_HOURS = 48
STALE_BLOCK_HOURS = 168  # 7 days

COMMIT_WARN_THRESHOLD = 20
COMMIT_BLOCK_THRESHOLD = 50

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObserveResult:
    """Outcome of pre-execution observation checks."""

    proceed: bool  # True = continue to REVIEWING, False = block
    annotations: list[str] = field(default_factory=list)
    block_reason: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def observe(
    *,
    created_at: str,
    repo_root: Path,
    active_tasks: list[dict],
    task_id: str,
) -> ObserveResult:
    """Run observation checks before committing to execution.

    Parameters
    ----------
    created_at:
        ISO timestamp of task creation (from task_states.created_at).
    repo_root:
        Path to the repository root for git commands.
    active_tasks:
        Pre-fetched list of recent task dicts (from list_all_recent).
    task_id:
        ID of the task being observed (excluded from overlap check).

    Returns
    -------
    ObserveResult with proceed=True/False and annotations/block_reason.
    """
    annotations: list[str] = []

    # --- Check 1: Plan age ---
    age_ann, age_block = _check_plan_age(created_at)
    if age_block:
        return ObserveResult(
            proceed=False,
            annotations=[age_ann] if age_ann else [],
            block_reason=age_ann or "Plan is critically stale",
        )
    if age_ann:
        annotations.append(age_ann)

    # --- Check 2: Git activity ---
    commit_count = await _count_commits_since(created_at, repo_root)
    if commit_count >= COMMIT_BLOCK_THRESHOLD:
        reason = (
            f"{commit_count} commits landed since task creation — "
            f"codebase may have changed significantly"
        )
        return ObserveResult(
            proceed=False,
            annotations=[reason],
            block_reason=reason,
        )
    if commit_count >= COMMIT_WARN_THRESHOLD:
        annotations.append(
            f"{commit_count} commits landed since task creation"
        )

    # --- Check 3: Task overlap ---
    overlap_annotations = _check_task_overlap(created_at, active_tasks, task_id)
    annotations.extend(overlap_annotations)

    return ObserveResult(proceed=True, annotations=annotations)


# ---------------------------------------------------------------------------
# Internal checks
# ---------------------------------------------------------------------------


def _check_plan_age(created_at: str) -> tuple[str | None, bool]:
    """Check if the task is stale based on creation time.

    Returns (annotation_or_None, should_block).
    """
    if not created_at:
        return None, False

    try:
        created = datetime.fromisoformat(created_at)
        # SQLite datetime('now') produces naive UTC strings
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = datetime.now(UTC) - created
        age_hours = age.total_seconds() / 3600
    except (ValueError, TypeError):
        logger.warning("Cannot parse created_at: %s", created_at)
        return None, False

    if age_hours >= STALE_BLOCK_HOURS:
        return (
            f"Plan is {age.days} days old (>{STALE_BLOCK_HOURS // 24}d threshold)"
        ), True

    if age_hours >= STALE_WARN_HOURS:
        return (
            f"Plan is {age_hours:.0f}h old (>{STALE_WARN_HOURS}h warning threshold)"
        ), False

    return None, False


async def _count_commits_since(created_at: str, repo_root: Path) -> int:
    """Count git commits since the given timestamp. Fail-open: returns 0."""
    if not created_at:
        return 0

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", f"--since={created_at}",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            logger.debug(
                "git log failed (returncode %d), assuming 0 commits",
                proc.returncode,
            )
            return 0
        # Count non-empty lines
        lines = [
            ln for ln in stdout.decode(errors="replace").splitlines()
            if ln.strip()
        ]
        return len(lines)
    except OSError:
        logger.warning(
            "git subprocess failed, assuming 0 commits", exc_info=True,
        )
        return 0


def _check_task_overlap(
    created_at: str,
    active_tasks: list[dict],
    task_id: str,
) -> list[str]:
    """Check for other tasks that completed since this task was created."""
    if not created_at or not active_tasks:
        return []

    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return []

    completed_since = []
    for t in active_tasks:
        if t.get("task_id") == task_id:
            continue
        phase = t.get("current_phase", "")
        if phase not in ("completed", "failed"):
            continue
        updated = t.get("updated_at", "")
        if not updated:
            continue
        try:
            updated_dt = datetime.fromisoformat(updated)
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=UTC)
            if updated_dt > created:
                completed_since.append(t.get("task_id", "unknown")[:8])
        except (ValueError, TypeError):
            continue

    if completed_since:
        return [
            f"{len(completed_since)} other task(s) completed since this "
            f"task was created ({', '.join(completed_since[:3])})"
        ]
    return []
