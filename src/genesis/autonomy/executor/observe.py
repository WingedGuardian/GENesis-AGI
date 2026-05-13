"""Pre-execution observation — context annotations for the reviewer.

Deterministic checks run before REVIEWING to provide context about
whether the task environment has drifted since the plan was written.
No LLM calls — git commands + in-memory task comparison only.

All checks are annotation-only — they NEVER block execution.
Annotations are injected into plan_content so the reviewer LLM
can factor them into its assessment.

Three checks:
1. Activity age: time since last task activity (updated_at)
2. Git activity: commits landed since last task activity
3. Task overlap: other tasks completed since this one was last active
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
COMMIT_WARN_THRESHOLD = 20

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObserveResult:
    """Outcome of pre-execution observation checks.

    Annotations only — this phase never blocks execution.
    """

    annotations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def observe(
    *,
    updated_at: str,
    repo_root: Path,
    active_tasks: list[dict],
    task_id: str,
) -> ObserveResult:
    """Run observation checks before committing to execution.

    Parameters
    ----------
    updated_at:
        ISO timestamp of last task activity (from task_states.updated_at).
        Measures time since last meaningful interaction, not submission time.
    repo_root:
        Path to the repository root for git commands.
    active_tasks:
        Pre-fetched list of recent task dicts (from list_all_recent).
    task_id:
        ID of the task being observed (excluded from overlap check).

    Returns
    -------
    ObserveResult with annotations (may be empty if everything is fresh).
    """
    annotations: list[str] = []

    # --- Check 1: Activity age ---
    age_ann = _check_activity_age(updated_at)
    if age_ann:
        annotations.append(age_ann)

    # --- Check 2: Git activity ---
    commit_count = await _count_commits_since(updated_at, repo_root)
    if commit_count >= COMMIT_WARN_THRESHOLD:
        annotations.append(
            f"{commit_count} commits landed since last task activity"
        )

    # --- Check 3: Task overlap ---
    overlap_annotations = _check_task_overlap(updated_at, active_tasks, task_id)
    annotations.extend(overlap_annotations)

    return ObserveResult(annotations=annotations)


# ---------------------------------------------------------------------------
# Internal checks
# ---------------------------------------------------------------------------


def _check_activity_age(updated_at: str) -> str | None:
    """Check time since last task activity. Returns annotation or None."""
    if not updated_at:
        return None

    try:
        updated = datetime.fromisoformat(updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        age = datetime.now(UTC) - updated
        age_hours = age.total_seconds() / 3600
    except (ValueError, TypeError):
        logger.warning("Cannot parse updated_at: %s", updated_at)
        return None

    if age_hours >= STALE_WARN_HOURS:
        if age.days >= 1:
            return f"No activity for {age.days} days"
        return f"No activity for {age_hours:.0f}h"

    return None


async def _count_commits_since(updated_at: str, repo_root: Path) -> int:
    """Count git commits since the given timestamp. Fail-open: returns 0."""
    if not updated_at:
        return 0

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", f"--since={updated_at}",
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
    updated_at: str,
    active_tasks: list[dict],
    task_id: str,
) -> list[str]:
    """Check for other tasks that completed since this task was last active."""
    if not updated_at or not active_tasks:
        return []

    try:
        updated = datetime.fromisoformat(updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return []

    completed_since = []
    for t in active_tasks:
        if t.get("task_id") == task_id:
            continue
        phase = t.get("current_phase", "")
        if phase not in ("completed", "failed"):
            continue
        t_updated = t.get("updated_at", "")
        if not t_updated:
            continue
        try:
            t_updated_dt = datetime.fromisoformat(t_updated)
            if t_updated_dt.tzinfo is None:
                t_updated_dt = t_updated_dt.replace(tzinfo=UTC)
            if t_updated_dt > updated:
                completed_since.append(t.get("task_id", "unknown")[:8])
        except (ValueError, TypeError):
            continue

    if completed_since:
        return [
            f"{len(completed_since)} other task(s) completed since last "
            f"activity ({', '.join(completed_since[:3])})"
        ]
    return []
